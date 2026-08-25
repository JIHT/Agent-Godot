"""tests/test_permission/test_gate_confirm.py —— M09 §5：决策门三分支 + 确认门。

覆盖：gate 三分支 / 批准执行 / 拒绝回填 DENIED Observation / 超时自动拒绝 /
"本次会话不再问" / Dispatcher 接线（deny 短路 & 确认门拦下）/
同批并行部分确认的恢复（已完成响应表，无二次副作用——§3 难点）。
"""
from __future__ import annotations

import time
from pathlib import Path

from agent_godot.core import ToolCall
from agent_godot.permission.confirm import (ConfirmAnswer, ConfirmGate,
                                            PendingConfirm, resume_batch)
from agent_godot.permission.gate import PermissionGate
from agent_godot.permission.risk import RiskLevel
from agent_godot.session import SessionManager, SessionState
from agent_godot.session.state import (AssistantMsg, ToolDone, UserInput)

from .conftest import (allow_session, approve, call, deny, make_gate,
                       make_registry, make_rules)


async def test_gate_three_branches():
    """allow / deny / need_confirm 三分支 + 未知工具拒绝。"""
    rules = make_rules({"rules": [{"tool": "delete_file", "action": "deny"}]})
    gate = PermissionGate(rules, None, registry=make_registry())

    assert (await gate.check(call("c1", "echo", x="hi"))).action == "allow"
    assert (await gate.check(call("c2", "delete_file", path="a.gd"))).action == "deny"
    assert (await gate.check(call("c3", "mark", path="a.gd"))).action == "need_confirm"
    assert (await gate.check(call("c4", "hallucinated_tool"))).action == "deny"


async def test_confirm_approve_executes(tmp_path: Path):
    """批准 → 现场执行（副作用发生）+ 事件流完整（Asked→Answered）。"""
    from agent_godot.session import Session

    session = Session("s1")
    gate = make_gate(session=session, prompter=approve)
    target = tmp_path / "a.txt"
    resp = await gate.request(call("c1", "mark", path=str(target), tag="t1"))
    assert resp.ok
    assert target.read_text(encoding="utf-8") == "t1\n"
    types = [type(e).__name__ for e in session.events]
    assert types == ["ConfirmAsked", "ConfirmAnswered"]
    assert session.state is SessionState.ACTIVE


async def test_confirm_deny_returns_observation(tmp_path: Path):
    """拒绝 → DENIED Observation（不抛异常），模型可据此改道。"""
    from agent_godot.session import Session

    session = Session("s1")
    gate = make_gate(session=session, prompter=deny)
    resp = await gate.request(call("c1", "mark", path=str(tmp_path / "a.txt")))
    assert not resp.ok
    assert resp.error.kind.value == "denied"
    assert "用户拒绝" in resp.render_for_model()
    assert "替代方案" in resp.render_for_model()      # hint 引导模型改道
    assert not (tmp_path / "a.txt").exists()          # 副作用没发生


async def test_remember_session_grants_rule(tmp_path: Path):
    """a = 本次会话不再问 → 命中规则进会话授权集合，第二次直跑。"""
    from agent_godot.session import Session

    session = Session("s1")
    rules = make_rules({"rules": [
        {"tool": "mark", "action": "ask", "remember": "session"}]})
    gate = make_gate(session=session, rules=rules, prompter=allow_session)
    target = tmp_path / "a.txt"
    await gate.request(call("c1", "mark", path=str(target)))
    assert rules.snapshot_for_resume()                # 授权已入集合
    # 第二次同工具调用：规则已翻绿，直接放行（不再进确认门）
    d = await gate.check(call("c2", "mark", path=str(target)))
    assert d.action == "allow"


async def test_timeout_auto_deny():
    """expires_at 到点 → 自动拒绝 + 会话收尾事件落库（防会话泄漏）。"""
    from agent_godot.session import Session

    session = Session("s1")
    gate = make_gate(session=session, timeout=0.05)   # 无 prompter → wait_resume 路径
    resp = await gate.request(call("c1", "mark", path="whatever.txt"))
    assert not resp.ok
    assert "超时" in resp.error.message
    assert session.state is SessionState.ACTIVE       # 已收尾回 active
    answered = [e for e in session.events
                if type(e).__name__ == "ConfirmAnswered"]
    assert answered and answered[0].approved is False


async def test_dispatcher_wiring_deny_and_confirm(tmp_path: Path):
    """M03 接线：gate 挂 Dispatcher——deny 短路、need_confirm 进门、allow 直跑。"""
    from agent_godot.agent.dispatcher import Dispatcher
    from agent_godot.session import Session

    rules = make_rules({"rules": [{"tool": "delete_file", "action": "deny"}]})
    reg = make_registry()
    dispatcher = Dispatcher(reg)
    session = Session("s1")
    gate = ConfirmGate(rules, session, dispatcher, registry=reg, prompter=approve)
    dispatcher.gate = gate

    target = tmp_path / "a.txt"
    calls = [call("c1", "echo", x="hi"),
             call("c2", "mark", path=str(target), tag="t1"),
             call("c3", "delete_file", path=str(target))]
    results = await dispatcher.execute(calls)

    assert [r.call_id for r in results] == ["c1", "c2", "c3"]   # 保序
    assert results[0].ok and "echo:hi" in results[0].summary
    assert results[1].ok and target.exists()                    # 批准后执行
    assert not results[2].ok                                    # deny 短路
    assert results[2].error.kind.value == "denied"
    assert target.exists()                                      # 没被删


async def test_partial_batch_confirm_resume(tmp_path: Path):
    """§3 核心难点：5 并行 calls，3 allow 已执行 + 1 拒 + 1 挂起待确认，
    批准挂起项后恢复——已执行的 3 个用事件流旧响应，绝不二次执行。"""
    from agent_godot.agent.dispatcher import Dispatcher

    project = tmp_path
    manager = SessionManager(project, db_path=project / "sessions.db")
    reg = make_registry()
    dispatcher = Dispatcher(reg)
    session = await manager.create()

    # ---- 模拟原始进程：一轮 5 个调用 ----
    fa = project / "a.txt"          # c2 被拒（标记不该出现）
    fb = project / "b.txt"          # c4 挂起待确认（恢复后执行一次）
    session.record(UserInput(text="批量操作"))
    session.record(AssistantMsg(tool_calls=[
        {"id": "c1", "name": "echo", "arguments": '{"x": "1"}'},
        {"id": "c2", "name": "mark", "arguments": f'{{"path": "{fa}", "tag": "c2"}}'},
        {"id": "c3", "name": "echo", "arguments": '{"x": "3"}'},
        {"id": "c4", "name": "mark", "arguments": f'{{"path": "{fb}", "tag": "c4"}}'},
        {"id": "c5", "name": "echo", "arguments": '{"x": "5"}'},
    ]))
    # 已完成的 3 个免确认 + 1 个被拒（事件流里有 ToolDone 记录）
    session.record(ToolDone(call_id="c1", tool="echo", ok=True, summary="echo:1"))
    session.record(ToolDone(call_id="c2", tool="mark", ok=False,
                            summary="[工具 mark 失败: denied] 用户拒绝执行"))
    session.record(ToolDone(call_id="c3", tool="echo", ok=True, summary="echo:3"))
    session.record(ToolDone(call_id="c5", tool="echo", ok=True, summary="echo:5"))
    # c4 挂起（进程在这里被杀）
    await session.suspend_with(PendingConfirm(
        call_id="c4", tool="mark", args={"path": str(fb), "tag": "c4"},
        risk=RiskLevel.MEDIUM, preview=None, expires_at=time.time() + 3600))

    # ---- "重启"：从事件流恢复，家属签字批准 ----
    restored = await manager.resume(session.session_id)
    assert restored.state is SessionState.WAITING_CONFIRM
    assert restored.pending_confirm.call_id == "c4"
    await manager.answer_confirm(session.session_id,
                                  ConfirmAnswer(approved=True),
                                  session=restored)

    done = await resume_batch(restored, dispatcher)

    # 已完成响应表：5 个 call 全有归宿
    assert set(done) == {"c1", "c2", "c3", "c4", "c5"}
    assert "echo:1" in done["c1"].summary            # 旧响应来自事件流
    assert not done["c2"].ok                          # 拒绝保留
    assert done["c4"].ok and fb.read_text(encoding="utf-8") == "c4\n"   # 现场执行恰好一次
    assert not fa.exists()                            # 被拒的从未执行
