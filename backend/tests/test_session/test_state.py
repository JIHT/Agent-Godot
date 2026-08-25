"""tests/test_session/test_state.py —— M09 §1.3：六态状态机 + 事件序列化。"""
from __future__ import annotations

import pytest

from agent_godot.core import Message, ToolCall
from agent_godot.permission.confirm import ConfirmAnswer, PendingConfirm
from agent_godot.permission.risk import RiskLevel
from agent_godot.session import InvalidTransition, SessionState
from agent_godot.session.state import (CompactDone, CompactStarted,
                                       ConfirmAnswered, ConfirmAsked,
                                       SessionClosed, SessionCreated,
                                       SessionError, UserInput, event_from_dict)


def _session():
    from agent_godot.session.manager import Session
    return Session("s1")


def _pc(call_id="c1") -> PendingConfirm:
    return PendingConfirm(call_id=call_id, tool="mark", args={"path": "a"},
                          risk=RiskLevel.MEDIUM, preview="预览", expires_at=0.0)


def test_transitions_full_lifecycle():
    s = _session()
    s.apply(SessionCreated(project_id="p1"))
    assert s.state is SessionState.ACTIVE
    s.apply(UserInput(text="hi"))
    s.apply(ConfirmAsked(call_id="c1", tool="mark", args={}, risk="medium"))
    assert s.state is SessionState.WAITING_CONFIRM
    assert s.pending_confirm.call_id == "c1"
    s.apply(ConfirmAnswered(call_id="c1", approved=True))
    assert s.state is SessionState.ACTIVE
    s.apply(CompactStarted())
    assert s.state is SessionState.COMPACTING
    s.apply(CompactDone(summary="压缩纪要"))
    assert s.state is SessionState.ACTIVE
    s.apply(SessionError(message="boom"))
    assert s.state is SessionState.ERROR
    s.apply(UserInput(text="/resume 重试"))          # error → active
    assert s.state is SessionState.ACTIVE
    s.apply(SessionClosed(final_text="done"))
    assert s.state is SessionState.CLOSED
    s.apply(UserInput(text="新的一天继续"))          # closed → active（续聊）
    assert s.state is SessionState.ACTIVE


def test_invalid_transition_user_input_while_waiting():
    s = _session()
    s.apply(UserInput(text="go"))
    s.apply(ConfirmAsked(call_id="c1", tool="mark", args={}, risk="high"))
    with pytest.raises(InvalidTransition):
        s.apply(UserInput(text="不该在这个状态进来"))


def test_invalid_transition_answer_without_ask():
    s = _session()
    with pytest.raises(InvalidTransition):
        s.apply(ConfirmAnswered(call_id="c9", approved=True))


def test_rewind_event_resets_to_active():
    from agent_godot.session.state import Rewind
    s = _session()
    s.apply(UserInput(text="第 1 轮"))
    s.apply(Rewind(turns=1, files=["a.gd"], task_ids=["t1"]))
    assert s.state is SessionState.ACTIVE           # rolled_back 瞬态后回 active
    assert s.rolled_back_turns == 1


def test_deterministic_replay():
    """同一事件序列回放两次 → 状态逐字段相等（确定性纪律，§1.3 易错点①）。"""
    events = [
        SessionCreated(project_id="p1"),
        UserInput(text="任务"),
        ConfirmAsked(call_id="c1", tool="mark", args={"path": "a"}, risk="medium"),
        ConfirmAnswered(call_id="c1", approved=False, reason="不要"),
    ]
    s1, s2 = _session(), _session()
    for e in events:
        s1.apply(e)
    for e in events:
        s2.apply(e)
    assert s1.snapshot_state() == s2.snapshot_state()


def test_append_message_translates_to_events():
    s = _session()
    s.append(Message(role="user", content="你好"))
    s.append(Message(role="assistant", content="我来看看",
                     tool_calls=[ToolCall(id="c1", name="echo", arguments="{}")]))
    s.append(Message(role="tool", tool_call_id="c1", content="echo:ok"))
    assert [type(e).__name__ for e in s.events] == [
        "UserInput", "AssistantMsg", "ToolDone"]
    assert s.messages[1].tool_calls[0].name == "echo"
    assert s.messages[2].content == "echo:ok"


def test_event_serialization_roundtrip():
    """to_dict → from_dict 往返不丢字段（SQLite 落库的前置条件）。"""
    events = [
        SessionCreated(project_id="p1"),
        UserInput(text="hi"),
        ConfirmAsked(call_id="c1", tool="mark", args={"path": "a"},
                     risk="medium", preview="p"),
        ConfirmAnswered(call_id="c1", approved=True, remember="session"),
        SessionClosed(final_text="bye"),
    ]
    for e in events:
        restored = event_from_dict(e.to_dict())
        assert type(restored) is type(e)
        assert restored.to_dict() == e.to_dict()


def test_unknown_event_type_rejected():
    with pytest.raises(ValueError):
        event_from_dict({"type": "NotAnEvent"})
