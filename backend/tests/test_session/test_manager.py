"""tests/test_session/test_manager.py —— M09 §1.3 / §5：事件溯源持久化与恢复。

杀进程重启后 resume 回放结果与崩溃前内存态一致（对比测试）；
挂起-答题-恢复全链路；Loop 兼容（append 即落盘）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_godot.core import Message
from agent_godot.permission.confirm import ConfirmAnswer, PendingConfirm
from agent_godot.permission.risk import RiskLevel
from agent_godot.session import InvalidTransition, SessionManager, SessionState
from agent_godot.session.state import (AssistantMsg, ToolDone, UserInput)


async def _rich_session(manager: SessionManager):
    """造一个有完整历史的会话：两轮对话 + 工具批 + 确认门往返。"""
    s = await manager.create("proj-x")
    s.record(UserInput(text="帮我改脚本"))
    s.record(AssistantMsg(content="好的", tool_calls=[
        {"id": "c1", "name": "mark", "arguments": '{"path": "a.gd"}'}]))
    s.record(ToolDone(call_id="c1", tool="mark", ok=True, summary="marked a.gd"))
    s.record(UserInput(text="再来一次"))
    s.record(AssistantMsg(content="这次要确认", tool_calls=[
        {"id": "c2", "name": "delete_file", "arguments": '{"path": "b.gd"}'}]))
    await s.suspend_with(PendingConfirm(
        call_id="c2", tool="delete_file", args={"path": "b.gd"},
        risk=RiskLevel.HIGH, preview="删除 b.gd", expires_at=0.0))
    await s.answer(ConfirmAnswer(approved=False, reason="别删"))
    return s


async def test_resume_after_crash_deterministic(tmp_path: Path):
    """杀进程重启：resume 回放结果与崩溃前内存态逐字段一致。"""
    manager = SessionManager(tmp_path, db_path=tmp_path / "sessions.db")
    s = await _rich_session(manager)
    before = s.snapshot_state()

    # "崩溃"：丢掉一切内存态，新 manager 从盘上恢复
    manager2 = SessionManager(tmp_path, db_path=tmp_path / "sessions.db")
    restored = await manager2.resume(s.session_id)
    assert restored.snapshot_state() == before

    # 再恢复一次仍然一致（确定性回放）
    restored2 = await manager2.resume(s.session_id)
    assert restored2.snapshot_state() == before


async def test_resume_waiting_confirm_then_answer(tmp_path: Path):
    """挂起中崩溃 → resume 停在 waiting_confirm → answer_confirm 回填续命。"""
    manager = SessionManager(tmp_path, db_path=tmp_path / "sessions.db")
    s = await manager.create()
    s.record(UserInput(text="go"))
    await s.suspend_with(PendingConfirm(
        call_id="c1", tool="mark", args={}, risk=RiskLevel.MEDIUM,
        preview=None, expires_at=0.0))

    manager2 = SessionManager(tmp_path, db_path=tmp_path / "sessions.db")
    restored = await manager2.resume(s.session_id)
    assert restored.state is SessionState.WAITING_CONFIRM
    assert restored.pending_confirm.call_id == "c1"

    out = await manager2.answer_confirm(s.session_id, ConfirmAnswer(approved=True))
    assert out.state is SessionState.ACTIVE
    assert out.pending_answer.approved is True

    # 落盘验证：第三次恢复能看到答案
    restored3 = await manager2.resume(s.session_id)
    assert restored3.pending_answer.approved is True


async def test_answer_confirm_rejects_non_waiting(tmp_path: Path):
    manager = SessionManager(tmp_path, db_path=tmp_path / "sessions.db")
    s = await manager.create()
    with pytest.raises(InvalidTransition):
        await manager.answer_confirm(s.session_id, ConfirmAnswer(approved=True))


async def test_loop_compatible_append_persists(tmp_path: Path):
    """AgentLoop 鸭类型兼容：session.append(Message) 即事件落盘。"""
    manager = SessionManager(tmp_path, db_path=tmp_path / "sessions.db")
    s = await manager.create()
    s.append(Message(role="user", content="问题"))
    s.append(Message(role="assistant", content="回答"))
    s.append(Message(role="tool", tool_call_id="c1", content="结果"))

    restored = await manager.resume(s.session_id)
    assert [(m.role, m.content) for m in restored.messages] == [
        ("user", "问题"), ("assistant", "回答"), ("tool", "结果")]
    assert restored.turns() == 1


async def test_resume_latest_and_missing(tmp_path: Path):
    manager = SessionManager(tmp_path, db_path=tmp_path / "sessions.db")
    with pytest.raises(KeyError):
        await manager.resume_latest()
    s1 = await manager.create()
    s2 = await manager.create()
    assert (await manager.resume_latest()).session_id == s2.session_id
    with pytest.raises(KeyError):
        await manager.resume("no-such-session")


async def test_events_of_batch_scope(tmp_path: Path):
    """批次切片：从最后一个带 tool_calls 的 AssistantMsg 起到当前。"""
    manager = SessionManager(tmp_path, db_path=tmp_path / "sessions.db")
    s = await manager.create()
    s.record(UserInput(text="go"))
    s.record(AssistantMsg(tool_calls=[
        {"id": "c1", "name": "echo", "arguments": "{}"}]))
    s.record(ToolDone(call_id="c1", tool="echo", ok=True, summary="ok"))
    batch = s.events_of_batch()
    assert [type(e).__name__ for e in batch] == ["ToolDone"]
    # events_since_suspend 为空（还没挂起过）
    assert s.events_since_suspend() == []
