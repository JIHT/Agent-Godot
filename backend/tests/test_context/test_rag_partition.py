"""M12 接线：rag 分区注入与降级（QueryEngine → ContextBuilder）。"""
from __future__ import annotations

from agent_godot.context import BudgetConfig, ContextBuilder, TokenCounter
from agent_godot.core import Message


class _Session:
    def __init__(self, messages: list[Message]):
        self.session_id = "test"
        self.messages = messages


def _builder(window: int = 20_000, rag_provider=None, **kw) -> ContextBuilder:
    return ContextBuilder(
        counter=TokenCounter(),
        config=BudgetConfig(window=window, reserved_output=1_000, **kw),
        rag_provider=rag_provider)


async def test_rag_partition_injected_into_context():
    """检索块进 rag 分区：build 输出含 <retrieved_context>。"""
    async def provider(session):
        return [Message(role="system",
                        content="<retrieved_context>body_entered 说明</retrieved_context>")]

    builder = _builder(rag_provider=provider)
    session = _Session([
        Message(role="system", content="你是 Godot 助手"),
        Message(role="user", content="Area2D 的信号？")])
    out = await builder.build(session)
    assert any("retrieved_context" in (m.content or "") for m in out)
    assert "rag" in builder.last_layout()


async def test_rag_partition_dropped_under_pressure():
    """预算紧时 rag 分区可整体丢（降级链：memory→rag→history 之后才轮到）。"""
    big = "<retrieved_context>" + "x" * 60_000 + "</retrieved_context>"

    async def provider(session):
        return [Message(role="system", content=big)]

    builder = _builder(window=2_000, rag_provider=provider)
    session = _Session([Message(role="user", content="问个问题")])
    out = await builder.build(session)          # 不抛 ContextOverflowError
    assert not any("retrieved_context" in (m.content or "") for m in out)


async def test_rag_provider_failure_does_not_break_build():
    """检索注入抛异常 → 分区留空，主流程照常（增强不是依赖）。"""

    async def provider(session):
        raise RuntimeError("retriever down")

    builder = _builder(rag_provider=provider)
    session = _Session([Message(role="user", content="你好")])
    out = await builder.build(session)
    assert len(out) >= 1


async def test_rag_survives_longer_than_memory():
    """同级优先级的牺牲顺序：memory 先丢，rag 保到最后（分区列表序）。

    记量口径（TokenCounter）：8000 个 ASCII 字符 ≈ 2000 tokens；
    窗口 5200 - 预留 1000 = 预算 4200，双注入区共 ~4000 → 触发滞后带
    （85%→70%），history 压缩后仍超 → 先丢 memory（列表序在前）。
    """
    async def memory(session):
        return [Message(role="system", content="<memory>" + "m" * 8_000)]

    async def rag(session):
        return [Message(role="system", content="<rag>" + "r" * 8_000)]

    builder = _builder(window=5_200, rag_provider=rag)
    builder.memory_provider = memory
    session = _Session([Message(role="user", content="问题")])
    out = await builder.build(session)
    contents = [m.content or "" for m in out]
    # rag 分区保住了，memory 先被牺牲
    assert any("<rag>" in c for c in contents)
    assert not any("<memory>" in c for c in contents)


async def test_default_no_rag_partition():
    """没配 provider：rag 分区为空，行为与 M07 完全一致。"""
    builder = _builder()
    session = _Session([Message(role="user", content="你好")])
    out = await builder.build(session)
    assert all("retrieved_context" not in (m.content or "") for m in out)
