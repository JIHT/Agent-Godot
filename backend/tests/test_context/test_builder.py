"""tests/test_context/test_builder.py —— M07 §5：预算总装 / pin 存活 / layout。"""
from __future__ import annotations

from agent_godot.context import (BudgetConfig, Compressor, ContextBuilder,
                                 HistoryConfig, HistoryManager, TokenCounter)
from agent_godot.core import Message, ToolCall

from .conftest import FakeSummaryLLM, make_session_messages


class _Session:
    """builder.build 只认 .messages（与 agent.Session 鸭子兼容）。"""

    def __init__(self, messages: list[Message]):
        self.session_id = "test"
        self.messages = messages


def _builder(window: int, **kw) -> ContextBuilder:
    counter = TokenCounter()
    cfg = BudgetConfig(window=window, reserved_output=1_000, **kw)
    return ContextBuilder(counter=counter,
                          compressor=Compressor(llm=FakeSummaryLLM(),
                                                model="cheap"),
                          config=cfg,
                          history=HistoryManager(counter,
                                                 HistoryConfig(
                                                     keep_recent_turns=3)),
                          model="test-model")


async def test_builder_never_exceeds_window():
    """50 轮会话（含肥 Observation）→ build 后不超预算，layout 可查。"""
    msgs = make_session_messages(turns=50, obs_chars=600)   # 每轮肥观察
    builder = _builder(window=20_000)
    counter = builder.counter
    total_raw = counter.estimate(msgs)
    assert total_raw > 15_000                               # 确实需要治理

    out = await builder.build(_Session(msgs))

    budget = 20_000 - 1_000                                 # window - reserved
    assert counter.estimate(out) <= budget
    layout = builder.last_layout()
    assert {"system", "memory", "history", "latest", "tools"} <= set(layout)
    # 治理确实发生了（要么压缩要么滚动）
    assert len(out) < len(msgs)


async def test_builder_keeps_recent_turns_intact():
    """最近 3 轮的 assistant/tool 消息保持原文（工作记忆不可压）。"""
    msgs = make_session_messages(turns=50, obs_chars=400)
    builder = _builder(window=25_000)
    out = await builder.build(_Session(msgs))
    tail_ids = {m.tool_call_id for m in msgs[-8:] if m.role == "tool"}
    out_ids = {m.tool_call_id for m in out if m.role == "tool"}
    assert tail_ids <= out_ids
    # 配对完整性
    ids_out = {tc.id for m in out if m.tool_calls for tc in m.tool_calls}
    ids_in = {m.tool_call_id for m in out if m.role == "tool"}
    assert ids_out == ids_in


async def test_pinned_survives_compression():
    """pin 的"用户约定"消息在 C 档压缩后仍原文存在。"""
    msgs = make_session_messages(turns=50, obs_chars=500)
    agreement = Message(role="user",
                        content="记住：所有脚本必须用 tabs 缩进，禁止空格")
    msgs.insert(2, agreement)
    builder = _builder(window=12_000)               # 小窗口强制走到 C 档
    builder.history.pin(agreement)

    out = await builder.build(_Session(msgs))

    contents = [m.content for m in out]
    assert any(c == agreement.content for c in contents)   # 原文逐字保留


async def test_builder_overflow_hard_fails():
    """降级链用尽（latest 本身爆窗）→ 截断兜底仍超 → ContextOverflowError。"""
    from agent_godot.context import ContextOverflowError

    msgs = [Message(role="system", content="s" * 20_000)]   # system 本身 ~5k token
    msgs.append(Message(role="user", content="go"))
    msgs.append(Message(role="assistant", tool_calls=[
        ToolCall(id="c1", name="read", arguments="{}")]))
    msgs.append(Message(role="tool", tool_call_id="c1", content="x" * 50_000))

    builder = _builder(window=6_000)               # 预算 5k < system 5k+ → 无解
    try:
        await builder.build(_Session(msgs))
        raised = False
    except ContextOverflowError:
        raised = True
    assert raised, "降级链用尽应硬失败而非静默丢 system"


async def test_builder_layout_and_calibrate():
    """layout 返回各分区 token；calibrate 用回执调整估算系数。"""
    msgs = make_session_messages(turns=10, obs_chars=200)
    builder = _builder(window=50_000)
    out = await builder.build(_Session(msgs))
    assert out
    layout = builder.last_layout()
    assert layout["system"] > 0 and layout["latest"] > 0

    ratio_before = builder.counter.cjk_ratio
    # 伪造一份"低估 20%"的回执 → 系数应被调大
    builder._last_estimate = 1_000
    builder.calibrate(1_250)
    assert builder.counter.cjk_ratio > ratio_before


async def test_builder_tools_counted_in_budget():
    """工具声明占 input token——预算按实测值扣减。"""
    from agent_godot.core import ToolSpec

    msgs = make_session_messages(turns=10, obs_chars=200)
    builder = _builder(window=50_000)
    specs = [ToolSpec(name="write_file", description="写文件工具 " * 80,
                      parameters={"type": "object",
                                  "properties": {"path": {"type": "string"}}})]
    await builder.build(_Session(msgs), tools=specs)
    assert builder.last_layout()["tools"] > 200
