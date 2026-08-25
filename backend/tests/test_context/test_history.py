"""tests/test_context/test_history.py —— M07 §5：保留配对 / pin / sweep。"""
from __future__ import annotations

from agent_godot.context import HistoryConfig, HistoryManager, TokenCounter
from agent_godot.core import Message

from .conftest import make_session_messages


def _ids_out(msgs: list[Message]) -> set[str]:
    return {tc.id for m in msgs if m.tool_calls for tc in m.tool_calls}


def _ids_in(msgs: list[Message]) -> set[str]:
    return {m.tool_call_id for m in msgs if m.role == "tool"}


def test_rolling_keeps_tool_pairing():
    """构造切在配对中间的 30 条序列 → rolled 后 calls/ids 集合相等（无孤悬）。"""
    msgs = make_session_messages(turns=10)              # 20+ 条消息
    assert len(msgs) > 12
    hm = HistoryManager(config=HistoryConfig(max_history_tokens=300))
    rolled = hm.rolling(msgs)
    assert len(rolled) < len(msgs)                      # 确实滚掉了旧的
    assert _ids_out(rolled) == _ids_in(rolled)          # 无孤悬
    assert rolled[0].role != "tool"                     # 头部无孤悬 tool


def test_rolling_never_breaks_pair_even_over_budget():
    """预算极小 → 宁可临时超也要带上完整配对（配对 > 预算）。"""
    msgs = make_session_messages(turns=5)
    hm = HistoryManager(config=HistoryConfig(max_history_tokens=1))
    rolled = hm.rolling(msgs)
    assert rolled, "至少保留最后一条"
    assert _ids_out(rolled) == _ids_in(rolled)


def test_pinned_survives_rolling():
    """pin 的用户约定消息在滑窗滚出旧消息后仍原文存在。"""
    msgs = make_session_messages(turns=10)
    agreement = msgs[1]                                 # 第一条 user："帮我加一个敌人"
    hm = HistoryManager(config=HistoryConfig(max_history_tokens=200))
    hm.pin(agreement)
    rolled = hm.rolling(msgs)
    assert agreement in rolled                          # 同一对象原文保留


def test_pin_lru_eviction():
    """超过 pinned_max(32) → 最旧的 pin 被 LRU 淘汰。"""
    hm = HistoryManager()
    msgs = [Message(role="user", content=f"m{i}") for i in range(40)]
    for m in msgs:
        hm.pin(m)
    assert hm.pinned_count == 32
    assert hm.is_pinned(msgs[-1])
    assert not hm.is_pinned(msgs[0])                    # 最旧的被淘汰


def test_sweep_replaces_old_observations_keeps_recent():
    """旧 tool 消息 shrink 成一行占位；最近 N 轮与 pinned 保持原文。"""
    msgs = make_session_messages(turns=6)
    hm = HistoryManager(config=HistoryConfig(keep_recent_turns=2))
    swept = hm.sweep_replace_observations(msgs)

    placeholders = [m for m in swept
                    if m.role == "tool" and m.content.startswith("<observation")]
    assert placeholders, "应有旧观察被占位替换"
    for m in placeholders:
        assert m.tool_call_id                           # 占位必须保留配对键
        assert "tool=" in m.content and "tokens=" in m.content

    # 最近 2 轮的 tool 消息保持原文
    recent_tools = [m for m in swept[-5:] if m.role == "tool"]
    assert recent_tools and all(
        not m.content.startswith("<observation") for m in recent_tools)

    # 配对完整性不受 sweep 影响
    assert _ids_out(swept) == _ids_in(swept)


def test_sweep_skips_pinned_tool():
    """pinned 的 tool 消息不被占位替换。"""
    msgs = make_session_messages(turns=5)
    old_tool = next(m for m in msgs if m.role == "tool")
    hm = HistoryManager(config=HistoryConfig(keep_recent_turns=1))
    hm.pin(old_tool)
    swept = hm.sweep_replace_observations(msgs)
    kept = next(m for m in swept if m.tool_call_id == old_tool.tool_call_id)
    assert kept.content == old_tool.content
