"""permission/confirm.py —— 确认门：挂起/恢复/超时（M09 §1.2 / §4 步骤 4）

手术签字制度：主刀医生（Agent）不能自己决定做不做高危手术——
- 拿着手术方案到谈话间：PendingConfirm 带足决策材料（diff/目标路径/风险等级），
  "是否允许 write_file? y/n" 这种不带材料的确认是失败设计；
- 家属考虑期间医生不站手术室干耗：session.suspend_with 落盘，进程可退；
- 签字（批准）→ 现场执行；拒签 → 拒绝也是信息——不抛异常，把"用户拒绝"
  作为 DENIED Observation 回填，模型据此改道（ReAct 纠错在人类反馈维度的延伸）。

两条答题通路（同一套事件落盘，审计不缺）：
- prompter：同进程交互（CLI 输入线程 / 测试注入），request() 直接等答案返回；
- 挂起恢复：无 prompter 时 await session.wait_resume()（Future 由外部 set），
  超时（默认 24h）自动按拒绝收尾——防会话泄漏。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from agent_godot.core import ToolCall
from agent_godot.tools import ErrorKind, ToolError, ToolResponse

from .gate import GateDecision, PermissionGate
from .risk import RiskLevel
from .rules import RuleEngine

if TYPE_CHECKING:
    from agent_godot.agent.dispatcher import Dispatcher


@dataclass
class ConfirmAnswer:
    """家属的签字：批不批 + 理由 + 是否"本次会话不再问"。"""
    approved: bool
    reason: str = ""
    remember: Literal["never", "session", "always"] = "never"


@dataclass
class PendingConfirm:
    """谈话间的手术方案：带足决策材料的待确认单。"""
    call_id: str
    tool: str
    args: dict
    risk: RiskLevel
    preview: str | None                # Diff/命令/目标路径预览
    expires_at: float                  # 超时自动拒绝（防会话泄漏）
    created_at: float = field(default_factory=time.time)

    def as_call(self) -> ToolCall:
        """还原成 ToolCall（批准后现场执行的入参）。"""
        return ToolCall(id=self.call_id, name=self.tool,
                        arguments=json.dumps(self.args, ensure_ascii=False))


def denied_response(call_id: str, tool: str, reason: str = "") -> ToolResponse:
    """拒绝不是异常，是 Observation：模型读到后会换方案/问意图/绕路。"""
    msg = "用户拒绝执行" + (f"（{reason}）" if reason else "")
    return ToolResponse(ok=False, call_id=call_id, error=ToolError(
        kind=ErrorKind.DENIED, tool=tool, message=msg,
        hint="询问用户希望如何调整，或提出替代方案"))


def _parse_args(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    try:
        v = json.loads(arguments) if arguments else {}
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# prompter 协议：async (pc) -> ConfirmAnswer（CLI 输入线程 / 测试注入）
Prompter = Callable[[PendingConfirm], Awaitable[ConfirmAnswer]]


class ConfirmGate(PermissionGate):
    """确认门 = 判定门（继承 check）+ 挂起恢复流程（request）。

    挂在 Dispatcher 上：check 放行的直跑，deny 的短路回填，need_confirm
    的进 request()——同批并行调用"部分待确认"由 Dispatcher 逐个过门解决。
    """

    def __init__(self, rules: RuleEngine, session, dispatcher: "Dispatcher", *,
                 registry=None, prompter: Prompter | None = None,
                 timeout: float = 86400.0, bus=None):
        super().__init__(rules, session, registry)
        self.dispatcher = dispatcher
        self.prompter = prompter
        self.timeout = timeout
        self.bus = bus

    async def request(self, call: ToolCall) -> ToolResponse:
        """§1.2 ③ 挂起-恢复核心流：构造 PC → 落盘挂起 → 等答案 → 批准执行/拒绝回填。"""
        risk = self.risk_of(call.name)
        pc = PendingConfirm(
            call_id=call.id, tool=call.name,
            args=_parse_args(call.arguments), risk=risk,
            preview=self._preview(call),
            expires_at=time.time() + self.timeout)
        await self.session.suspend_with(pc)            # 状态机→waiting_confirm 并落盘
        if self.bus is not None:
            await self.bus.emit("confirm_requested", call_id=pc.call_id,
                                tool=pc.tool, risk=pc.risk.value, preview=pc.preview)

        if self.prompter is not None:
            outcome = self.prompter(pc)              # 同进程交互通路（兼容同步/异步）
            answer = outcome if isinstance(outcome, ConfirmAnswer) else await outcome
        else:
            try:
                answer = await asyncio.wait_for(
                    self.session.wait_resume(),
                    timeout=max(0.05, pc.expires_at - time.time()))
            except asyncio.TimeoutError:
                # 超时策略：挂起 24h 未答自动拒绝收尾（§1.2 易错点③）
                answer = ConfirmAnswer(approved=False, reason="确认超时，自动拒绝")
        await self.session.answer(answer)              # ConfirmAnswered 事件落盘

        if answer.approved and answer.remember == "session":
            self._grant_session_rule(call, risk)       # "本次会话不再问"
        if answer.approved:
            return await self.dispatcher.execute_now(call)
        resp = denied_response(call.id, call.name, answer.reason)
        self.dispatcher.report_result(call, resp)      # 拒绝也是一次完成，记账
        return resp

    def _grant_session_rule(self, call: ToolCall, risk: RiskLevel) -> None:
        """把命中的 ask 规则指纹进会话授权集合（快照随会话持久化）。"""
        d = self.rules.decide(call.name, call.arguments, risk=risk.value)
        if d.matched_rule:
            self.rules.grant_session(d.matched_rule)

    def _preview(self, call: ToolCall) -> str | None:
        """决策材料：目标路径 + 参数摘要（写类操作的"切哪里、有什么风险"）。"""
        args = _parse_args(call.arguments)
        lines = [f"工具: {call.name}（风险: {self.risk_of(call.name).value}）"]
        target = next((args[k] for k in ("path", "file", "scene", "target")
                       if isinstance(args.get(k), str)), None)
        if target:
            lines.append(f"目标: {target}")
        compact = json.dumps(args, ensure_ascii=False)
        if len(compact) > 500:
            compact = compact[:500] + "..."
        lines.append(f"参数: {compact}")
        return "\n".join(lines)


async def resume_batch(session, dispatcher: "Dispatcher") -> dict[str, ToolResponse]:
    """§3 恢复点的精确重构："已完成响应表"。

    挂起时同批 5 个调用：3 个已执行（事件流里有 ToolDoneEvent 记录 call_id
    与响应）、1 个待确认。恢复函数按事件流重建 done 表——已执行的用旧响应
    **绝不二次执行**（文件 mtime 不变是验收断言）；被批准的现场执行、被拒的
    构造 DENIED 响应。Loop 拿到 done 表从"观察回填"步继续。
    """
    pc = session.pending_confirm
    answer = session.pending_answer
    if pc is None or answer is None:
        raise ValueError("会话不在待确认恢复点（无 pending_confirm/answer）")
    # 惰性导入：session.manager 反向依赖本模块，顶层互 import 会成环
    from agent_godot.session.state import ToolDone

    done: dict[str, ToolResponse] = {}
    # 同批已完成调用的 ToolDone 记在挂起点之前（即时记账），向前覆盖整批
    batch_events = (session.events_of_batch()
                    if hasattr(session, "events_of_batch")
                    else session.events_since_suspend())
    for e in batch_events:
        if isinstance(e, ToolDone):
            done[e.call_id] = ToolResponse(ok=e.ok, call_id=e.call_id,
                                           summary=e.summary)
    if answer.approved:
        done[pc.call_id] = await dispatcher.execute_now(pc.as_call())
    else:
        done[pc.call_id] = denied_response(pc.call_id, pc.tool, answer.reason)
    return done
