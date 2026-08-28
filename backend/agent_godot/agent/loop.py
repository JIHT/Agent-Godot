"""agent/loop.py —— ReAct 主循环本体（M03 §3 / §4 步骤 4）

心脏起搏器：想一步 → 做一步（调工具）→ 看一眼结果 → 再想，直到完成。
六步顺序就是全部设计（§3 难点）：
  ① 预算检查前置 → ② 重建上下文 → ③ 流式推理（事件直通）→ ④ 无 calls 自然终止
  → ⑤ 死循环劝导 → ⑥ 执行工具回填
任何一步挪位置都会引入"超支一轮 / 丢一次观察"的 bug。

★ 本循环是**四模式共用的底座**（M13 §1.3 范式×模式矩阵里 ReAct 一栏四模式
全为"✅ 强制"）。ask/craft/plan/multi 不是四套循环，而是挂在本循环上的
四组"契约配置"（策略对象）：改的是工具视图、采样参数、钩子，循环本体不动。

同理，本循环不是"ask 模式专属"——craft 的验证回路、plan 的 DAG 节点执行
（loop.run(mode="craft")）都复用这一个循环。模式与范式是两层正交的概念，
详见 M13 §1.3。

消费 M02 的 StreamEvent（adapter 已把 SSE 翻译成统一事件），不再碰 StreamAggregator。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from agent_godot.core import LLM, LLMRequest, Message, StreamEvent, ToolCall, Usage
from agent_godot.hooks import HookContext, HookPipeline, HookVeto
from .budgets import BudgetTracker, LoopDetector
from .dispatcher import Dispatcher
from .events import EventBus

if TYPE_CHECKING:
    from agent_godot.tools import ToolResponse


@dataclass
class Session:
    """最小会话（M09 正式化前的占位）：session_id + 消息序列。"""
    session_id: str
    messages: list[Message] = field(default_factory=list)

    def append(self, msg: Message) -> None:
        self.messages.append(msg)


class ContextBuilder:
    """上下文拼装基类（M07 前简单版：全量消息直出）。

    M07 的正式版是 agent_godot.context.ContextBuilder（分区预算+贪心降级，
    与本类鸭子类型兼容：build(session, tools=...)），构造 AgentLoop 时注入。
    """

    async def build(self, session: Session, *,
                    tools: list | None = None) -> list[Message]:
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
                 config: LoopConfig | None = None,
                 context: ContextBuilder | None = None,
                 verify_runner=None, approver=None,
                 hooks: HookPipeline | None = None):
        self.llm = llm
        self.dispatcher = dispatcher
        self.model = model
        self.temperature = temperature
        self.bus = bus or EventBus()
        self.config = config or LoopConfig()
        # M07：可注入 context.ContextBuilder（分区预算+压缩）；默认简单拼接
        self.context = context or ContextBuilder()
        self.budgets = BudgetTracker(
            max_steps=self.config.max_steps,
            token_budget=self.config.token_budget,
            usd_budget=self.config.usd_budget,
            wall_time_budget=self.config.wall_time_budget)
        self.detector = LoopDetector()
        self._loop_warnings = 0
        # M13：模式策略装配资源——craft 的客观验证器 / plan 的审批回调
        self.verify_runner = verify_runner
        self.approver = approver
        # M14：HookPipeline（pre_loop 可注入消息 / post_loop 可上报）。
        # pre_tool、post_tool 挂在 Dispatcher 上（工具级横切），不在这里。
        self.hooks = hooks
        self._strategy = None

    async def run(self, session: Session, user_input: str | None,
                  *, mode: str = "ask", strategy=None) -> LoopResult:
        """从用户输入到最终回答的完整发动机。

        M13：按 mode 装配策略对象（换挡器）——工具视图 / 采样参数 /
        循环钩子全由 strategy 决定，循环本体零改动。
        """
        self._strategy = strategy or self._make_strategy(mode)
        self.budgets.reset()
        self._loop_warnings = 0
        if user_input:
            session.append(Message(role="user", content=user_input))
            await self.bus.emit("user_message", content=user_input)
        return await self._drive(session, user_input)

    def _make_strategy(self, mode: str):
        """按模式实例化策略（注入循环已有的资源：llm/loop/验证器/审批回调）。"""
        from .paradigms import get_strategy
        return get_strategy(mode, llm=self.llm, loop=self, runner=self.verify_runner,
                            approver=self.approver)

    async def continue_with(self, session: Session,
                            done: dict[str, "ToolResponse"]) -> LoopResult:
        """M09 §3 恢复点续跑：先回填"已完成响应表"，再继续主循环。

        确认门挂起恢复后调用——同批已执行的调用用 done 表里的旧响应，
        **绝不重放副作用**；Loop 从"观察回填"这一步接着跑。
        """
        self.budgets.reset()
        self._loop_warnings = 0
        if self._strategy is None:                       # resume 前未 run 过
            self._strategy = self._make_strategy("ask")
        # 已被 dispatcher.on_result 即时落盘的调用不重复回填（M09 §3 防副作用重放）
        recorded = self._recorded_call_ids(session)
        for call_id, resp in done.items():
            if call_id not in recorded:
                session.append(Message(role="tool", tool_call_id=call_id,
                                       content=resp.render()))
            await self.bus.emit("tool_call_result", call_id=call_id,
                                ok=resp.ok, content=resp.render())
        return await self._drive(session)

    @staticmethod
    def _recorded_call_ids(session) -> set[str]:
        """session 事件流里已有 ToolDone 记录的 call_id（非事件溯源 session 返回空）。"""
        events = getattr(session, "events", None)
        if not events:
            return set()
        done_type = "ToolDone"
        return {e.call_id for e in events
                if type(e).__name__ == done_type}

    async def _drive(self, session: Session,
                     task: str | None = None) -> LoopResult:
        """主循环本体（run 与 continue_with 的公共发动机）。

        M13：装配策略——工具视图（能力边界）/ 采样参数 / 系统提示 /
        前置消息 / 写后验证注入 / 推进控制，全由 strategy 决定。
        """
        strategy = getattr(self, "_strategy", None)
        active_registry = self.dispatcher.registry
        original_registry = None
        temperature = self.temperature
        top_p = 0.95
        if strategy is not None:
            active_registry = strategy.tools_view(self.dispatcher.registry)
            # 物理能力边界：dispatcher 执行也用裁剪视图（ask 模式写工具不可达）
            if active_registry is not self.dispatcher.registry:
                original_registry = self.dispatcher.registry
                self.dispatcher.registry = active_registry
            if strategy.config.system_prompt_template:
                session.append(Message(role="system",
                                       content=strategy.config.system_prompt_template))
            for m in await strategy.before_loop(session, task or ""):
                session.append(m)
            temperature = strategy.config.temperature
            top_p = strategy.config.top_p
        # M14：pre_loop 钩子（预算告警/记忆召回等注入消息）；策略消息在前，
        # hook 消息在后——hook 是横切层，可以看见并覆盖策略层的注入
        if self.hooks is not None and self.hooks.has("pre_loop"):
            ctx = await self.hooks.run(
                "pre_loop", HookContext(point="pre_loop", session=session))
            for m in ctx.messages:
                session.append(m)
        try:
            while True:
                # 推进控制（plan/multi 扩展点，默认 True）
                if strategy is not None and not await strategy.should_continue(session):
                    return LoopResult("策略停止推进", self.budgets.steps,
                                      None, "natural")
                # ① 预算检查前置（工具本身可能跑 5 分钟，检查晚了等于没检查）
                status = self.budgets.check()
                if status.exhausted:
                    return await self._graceful_wrap_up(session, status)

                # ② 每轮重建上下文（M07：分区预算 + 贪心降级拼装）
                messages = await self.context.build(
                    session, tools=active_registry.tool_specs() or None)
                await self._emit_layout()

                req = LLMRequest(
                    model=self.model, messages=messages,
                    temperature=temperature, top_p=top_p,
                    tools=active_registry.tool_specs() or None)

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
                    self._calibrate(usage.input_tokens)

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
                for call, r in zip(calls, results):
                    # M09：dispatcher.on_result 即时记账时 Loop 不再补记（防重复落盘）
                    if self.dispatcher.on_result is None:
                        session.append(Message(role="tool", tool_call_id=r.call_id,
                                               content=r.render()))
                    await self.bus.emit("tool_call_result", call_id=r.call_id,
                                        ok=r.ok, content=r.render())
                    # M13 craft 验证注入：写后校验错误 → Observation 回填下一轮
                    if strategy is not None:
                        feedback = await strategy.on_tool_done(call.name, r, session)
                        if feedback:
                            session.append(Message(role="system", content=feedback))
                self.budgets.record_step()
        finally:
            # M14：post_loop 钩子（死循环上报/统计）。veto 在这里没有语义
            # （没有"被否决的执行"），吞掉即可——不能让它盖掉真实结果。
            if self.hooks is not None and self.hooks.has("post_loop"):
                try:
                    await self.hooks.run(
                        "post_loop",
                        HookContext(point="post_loop", session=session,
                                    extra={"steps": self.budgets.steps}))
                except HookVeto:
                    pass
            if original_registry is not None:
                self.dispatcher.registry = original_registry

    async def _forward(self, ev: StreamEvent) -> None:
        """流事件直通到 bus（text_delta 透传文本，done/usage 由 loop 自己消费）。"""
        if ev.type == "text_delta" and ev.text:
            await self.bus.emit("text_delta", text=ev.text)

    async def _emit_layout(self) -> None:
        """M07 trace：上轮各分区 token 占比（--trace 可查）。"""
        layout_fn = getattr(self.context, "last_layout", None)
        if callable(layout_fn):
            layout = layout_fn()
            if layout:
                await self.bus.emit("context_layout", layout=layout)

    def _calibrate(self, reported_input_tokens: int) -> None:
        """M07 自校准：真实 usage 回执 vs 估算值 → 回归调整估算系数。"""
        cal_fn = getattr(self.context, "calibrate", None)
        if callable(cal_fn):
            cal_fn(reported_input_tokens)

    async def _graceful_wrap_up(self, session: Session, status) -> LoopResult:
        """预算耗尽 → 优雅收尾：注入"请总结"做最后一轮（不调工具），而非抛异常。"""
        reason = status.reason or "timeout"
        session.append(Message(
            role="system",
            content=f"预算将尽（{reason}），请用一两句话总结你已完成的工作"
                    f"与尚未完成的事项，不要再调用工具。"))
        messages = await self.context.build(session)   # 收尾轮同样过预算治理
        req = LLMRequest(model=self.model, messages=messages,
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
