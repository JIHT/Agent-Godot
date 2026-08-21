"""agent/loop.py —— ReAct 主循环本体（M03 §3 / §4 步骤 4）

心脏起搏器：想一步 → 做一步（调工具）→ 看一眼结果 → 再想，直到完成。
六步顺序就是全部设计（§3 难点）：
  ① 预算检查前置 → ② 重建上下文 → ③ 流式推理（事件直通）→ ④ 无 calls 自然终止
  → ⑤ 死循环劝导 → ⑥ 执行工具回填
任何一步挪位置都会引入"超支一轮 / 丢一次观察"的 bug。

消费 M02 的 StreamEvent（adapter 已把 SSE 翻译成统一事件），不再碰 StreamAggregator。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from agent_godot.core import LLM, LLMRequest, Message, StreamEvent, ToolCall, Usage
from .budgets import BudgetTracker, LoopDetector
from .dispatcher import Dispatcher
from .events import EventBus


@dataclass
class Session:
    """最小会话（M09 正式化前的占位）：session_id + 消息序列。"""
    session_id: str
    messages: list[Message] = field(default_factory=list)

    def append(self, msg: Message) -> None:
        self.messages.append(msg)


class ContextBuilder:
    """上下文拼装（M07 前简单版：全量消息直出）。

    M07 会升级为：Observation 截断 + 滑动窗口 + 摘要压缩 + 预算分配拼装。
    """

    async def build(self, session: Session) -> list[Message]:
        return session.messages


@dataclass
class LoopConfig:
    """循环仪表盘预设。"""
    max_steps: int = 25
    token_budget: int = 200_000
    usd_budget: float = 0.5
    wall_time_budget: float = 600.0


@dataclass
class LoopResult:
    """一次循环的最终结果。stop_reason 决定前端怎么展示。"""
    final_text: str
    steps: int
    usage_total: Usage | None
    stop_reason: Literal["natural", "max_steps", "token_budget",
                         "usd_budget", "timeout", "loop_detected", "error"]


class AgentLoop:
    def __init__(self, llm: LLM, dispatcher: Dispatcher, *,
                 model: str, temperature: float = 0.3,
                 bus: EventBus | None = None,
                 config: LoopConfig | None = None):
        self.llm = llm
        self.dispatcher = dispatcher
        self.model = model
        self.temperature = temperature
        self.bus = bus or EventBus()
        self.config = config or LoopConfig()
        self.context = ContextBuilder()
        self.budgets = BudgetTracker(
            max_steps=self.config.max_steps,
            token_budget=self.config.token_budget,
            usd_budget=self.config.usd_budget,
            wall_time_budget=self.config.wall_time_budget)
        self.detector = LoopDetector()
        self._loop_warnings = 0

    async def run(self, session: Session, user_input: str | None,
                  *, mode: str = "ask") -> LoopResult:
        """从用户输入到最终回答的完整发动机。"""
        self.budgets.reset()
        self._loop_warnings = 0
        if user_input:
            session.append(Message(role="user", content=user_input))
            await self.bus.emit("user_message", content=user_input)

        while True:
            # ① 预算检查前置（工具本身可能跑 5 分钟，检查晚了等于没检查）
            status = self.budgets.check()
            if status.exhausted:
                return await self._graceful_wrap_up(session, status)

            # ② 每轮重建上下文
            messages = await self.context.build(session)
            req = LLMRequest(
                model=self.model, messages=messages,
                temperature=self.temperature,
                tools=self.dispatcher.registry.tool_specs() or None)

            # ③ 流式推理（消费 M02 统一事件，直通 bus）
            text_parts: list[str] = []
            calls: list[ToolCall] = []
            usage: Usage | None = None
            try:
                async for ev in self.llm.stream(req):
                    await self._forward(ev)
                    if ev.type == "text_delta" and ev.text:
                        text_parts.append(ev.text)
                    elif ev.type == "usage" and ev.usage:
                        usage = ev.usage
                    elif ev.type == "done":
                        calls = ev.tool_calls or []
            except Exception as e:                    # 流级错误 → 终止
                await self.bus.emit("error", error=str(e))
                return LoopResult("".join(text_parts), self.budgets.steps,
                                  usage, "error")

            if usage:
                self.budgets.record_usage(usage)

            # ④ 自然终止：无工具调用 = Final Answer
            if not calls:
                final = "".join(text_parts)
                await self.bus.emit("message_end", text=final,
                                    stop_reason="natural", usage=_usage_dict(usage))
                return LoopResult(final, self.budgets.steps, usage, "natural")

            # ⑤ 死循环劝导（第一次劝导给模型自救机会，第二次硬停）
            if self.detector.check(calls):
                self._loop_warnings += 1
                if self._loop_warnings >= 2:
                    final = "".join(text_parts)
                    await self.bus.emit("message_end", text=final,
                                        stop_reason="loop_detected")
                    return LoopResult(final, self.budgets.steps, usage,
                                      "loop_detected")
                session.append(Message(
                    role="system",
                    content="检测到你在重复调用相同的工具，请换一个思路，"
                            "或直接基于已有信息给出总结。"))
                await self.bus.emit("loop_warning")
                continue                             # 不执行工具，让模型重新决策

            # ⑥ 执行工具并回填 Observation
            session.append(Message(role="assistant", tool_calls=calls))
            await self.bus.emit("tool_call_start", calls=[c.name for c in calls])
            results = await self.dispatcher.execute(calls)
            for r in results:
                session.append(Message(role="tool", tool_call_id=r.call_id,
                                       content=r.render()))
                await self.bus.emit("tool_call_result", call_id=r.call_id,
                                    ok=r.ok, content=r.render())
            self.budgets.record_step()

    async def _forward(self, ev: StreamEvent) -> None:
        """流事件直通到 bus（text_delta 透传文本，done/usage 由 loop 自己消费）。"""
        if ev.type == "text_delta" and ev.text:
            await self.bus.emit("text_delta", text=ev.text)

    async def _graceful_wrap_up(self, session: Session, status) -> LoopResult:
        """预算耗尽 → 优雅收尾：注入"请总结"做最后一轮（不调工具），而非抛异常。"""
        reason = status.reason or "timeout"
        session.append(Message(
            role="system",
            content=f"预算将尽（{reason}），请用一两句话总结你已完成的工作"
                    f"与尚未完成的事项，不要再调用工具。"))
        req = LLMRequest(model=self.model, messages=session.messages,
                         temperature=self.temperature, tools=None)
        text_parts: list[str] = []
        usage: Usage | None = None
        async for ev in self.llm.stream(req):
            await self._forward(ev)
            if ev.type == "text_delta" and ev.text:
                text_parts.append(ev.text)
            elif ev.type == "usage" and ev.usage:
                usage = ev.usage
        if usage:
            self.budgets.record_usage(usage)   # ★ 收尾轮也要记账（§1.2 易错点③）
        final = "".join(text_parts)
        await self.bus.emit("message_end", text=final,
                            stop_reason=reason, truncated=True)
        return LoopResult(final, self.budgets.steps, usage, reason)


def _usage_dict(usage: Usage | None) -> dict:
    if usage is None:
        return {}
    return {"input": usage.input_tokens, "output": usage.output_tokens,
            "cost_usd": usage.cost_usd}
