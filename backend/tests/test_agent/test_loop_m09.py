"""tests/test_agent/test_loop_m09.py —— M09 接线端到端：Loop × 权限门 × 事件溯源。

两个场景：
① 带确认门的完整 loop.run（prompter 批准）——事件全量落盘，重启恢复一致；
② 挂起中"崩溃" → resume → 签字 → resume_batch → loop.continue_with——
   从观察回填步继续，已执行调用无二次副作用、tool_call/tool 消息配对完整。
"""
from __future__ import annotations

import json
from pathlib import Path

from agent_godot.agent import AgentLoop, Dispatcher
from agent_godot.core import Message, ToolCall
from agent_godot.permission.confirm import (ConfirmAnswer, ConfirmGate,
                                            PendingConfirm, resume_batch)
from agent_godot.permission.risk import RiskLevel
from agent_godot.permission.rules import RuleEngine
from agent_godot.session import SessionManager, SessionState
from agent_godot.session.state import AssistantMsg, ToolDone, UserInput
from agent_godot.tools import ToolRegistry, ToolResponse

from .conftest import EchoTool, FakeLLM, done_ev, text_ev


class MarkTool(EchoTool):
    """写工具（medium）：追加标记，副作用可数。"""
    meta = None

    class Params(EchoTool.Params):
        path: str = "marks.txt"

    async def run(self, path: str = "marks.txt", x: str = "ok") -> ToolResponse:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write("x\n")
        return ToolResponse(ok=True, summary=f"marked {path}")


def _attach(tool_cls, name, readonly, risk):
    from agent_godot.tools import ToolMeta
    tool_cls.meta = ToolMeta(name=name, description=name,
                             readonly=readonly, risk=risk)
    return tool_cls


def _stack(tmp_path: Path, *, prompter):
    """组装 M09 全家桶：registry + rules + manager + 挂门 dispatcher。

    setup() 创建事件溯源会话并完成 gate/on_result 双向绑定。
    """
    reg = ToolRegistry()
    reg.register(_attach(EchoTool, "echo", True, "low")())
    reg.register(_attach(MarkTool, "mark", False, "medium")())
    rules = RuleEngine(config={"rules": [{"tool": "mark", "action": "ask"}]})
    manager = SessionManager(tmp_path, db_path=tmp_path / "sessions.db")
    dispatcher = Dispatcher(reg)

    async def setup():
        s = await manager.create()
        gate = ConfirmGate(rules, s, dispatcher, registry=reg, prompter=prompter)
        dispatcher.gate = gate
        dispatcher.on_result = lambda call, resp: s.append(Message(
            role="tool", tool_call_id=call.id, content=resp.render()))
        return s
    return reg, rules, manager, dispatcher, setup


async def test_loop_with_confirm_gate_events_persisted(tmp_path: Path):
    """场景①：echo 直跑 + mark 过确认门（批准）→ 事件全量落盘可恢复。"""
    target = tmp_path / "marks.txt"

    async def approve(pc):
        return ConfirmAnswer(approved=True)

    reg, rules, manager, dispatcher, setup = _stack(tmp_path, prompter=approve)
    session = await setup()

    script = [
        [done_ev([ToolCall(id="c1", name="echo", arguments='{"x": "hi"}'),
                  ToolCall(id="c2", name="mark",
                           arguments=json.dumps({"path": str(target)}))],
                 "tool_calls")],
        [text_ev("两步都完成了"), done_ev(None, "stop")],
    ]
    loop = AgentLoop(FakeLLM(script), dispatcher, model="m")
    # M13：默认 ask 模式只读（写工具被 tools_view 物理过滤），确认门测试需写工具，
    # 故显式用 craft（全工具 + 无验证器时 VerifyLoop 自动跳过）。
    result = await loop.run(session, "干活", mode="craft")
    assert result.stop_reason == "natural"
    assert target.read_text(encoding="utf-8") == "x\n"      # 批准后恰好执行一次

    # 重启恢复：事件流回放与崩溃前逐字段一致
    restored = await manager.resume(session.session_id)
    assert restored.snapshot_state() == session.snapshot_state()


async def test_loop_crash_resume_continue_with(tmp_path: Path):
    """场景②：挂起中崩溃 → resume 签字 → continue_with 从观察回填步续跑。"""
    target = tmp_path / "marks.txt"

    async def approve(pc):
        return ConfirmAnswer(approved=True)

    reg, rules, manager, dispatcher, setup = _stack(tmp_path, prompter=approve)
    session = await setup()

    # 原始进程：一轮 [echo(allow), mark(待确认)]，echo 完成后挂起被杀
    session.record(UserInput(text="干活"))
    session.record(AssistantMsg(tool_calls=[
        {"id": "c1", "name": "echo", "arguments": '{"x": "hi"}'},
        {"id": "c2", "name": "mark",
         "arguments": json.dumps({"path": str(target)})},
    ]))
    session.record(ToolDone(call_id="c1", tool="echo", ok=True, summary="echo:hi"))
    await session.suspend_with(PendingConfirm(
        call_id="c2", tool="mark", args={"path": str(target)},
        risk=RiskLevel.MEDIUM, preview=None, expires_at=0.0))

    # 重启：恢复 → 批准 → 已完成响应表 → continue_with
    restored = await manager.resume(session.session_id)
    assert restored.state is SessionState.WAITING_CONFIRM
    await manager.answer_confirm(session.session_id,
                                  ConfirmAnswer(approved=True), session=restored)
    done = await resume_batch(restored, dispatcher)

    script = [[text_ev("好的，已按批准继续完成"), done_ev(None, "stop")]]
    loop = AgentLoop(FakeLLM(script), dispatcher, model="m")
    result = await loop.continue_with(restored, done)
    assert result.stop_reason == "natural"

    # 副作用恰好一次；观察回填无重复
    assert target.read_text(encoding="utf-8") == "x\n"
    tool_msgs = [m for m in restored.messages if m.role == "tool"]
    assert len(tool_msgs) == 2                                # c1 + c2 各一条
    # tool_call / tool 消息配对完整（协议要求每个 call 都有响应）
    call_ids = {c.id for m in restored.messages
                if m.role == "assistant" and m.tool_calls for c in m.tool_calls}
    assert call_ids == {"c1", "c2"}
    assert {m.tool_call_id for m in tool_msgs} == call_ids
