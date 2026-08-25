"""tests/test_memory/test_integration.py —— M08 §5 验收：跨会话约定生效 + M07 接入。"""
from __future__ import annotations

import json

from agent_godot.agent import Session
from agent_godot.context import BudgetConfig, ContextBuilder, TokenCounter
from agent_godot.core import Message
from agent_godot.memory import (MemoryExtractor, MemoryRetriever, RecallConfig,
                                MemoryStore, fake_embed, make_memory_provider)

from .conftest import FakeExtractLLM, make_session_messages, make_store


async def test_memory_provider_populates_context():
    """M07 接入：memory_provider 把记忆注入 ContextBuilder 的 memory 分区。"""
    store = make_store()
    # 预置一条约定记忆
    from agent_godot.memory import MemoryRecord
    await store.add(MemoryRecord.make(
        "semantic", "用户偏好：信号回调命名 _on_x_y",
        project_id="proj-a", importance=0.9))

    retriever = MemoryRetriever(store, config=RecallConfig(k=5, max_tokens=2000))
    provider = make_memory_provider(retriever, "proj-a")

    context = ContextBuilder(
        counter=TokenCounter(),
        config=BudgetConfig(),
        memory_provider=provider)

    session = Session(session_id="s1", messages=[
        Message(role="system", content="你是 Godot Agent。"),
        Message(role="user", content="帮我给按钮加信号回调"),
    ])
    messages = await context.build(session, tools=None)

    # memory 分区应被注入（在 system 之后）
    memory_content = "\n".join(m.content or "" for m in messages
                               if m.role == "system" and "<project_memory>" in (m.content or ""))
    assert "<project_memory>" in memory_content
    assert "_on_x_y" in memory_content
    store.close()


async def test_memory_provider_fail_soft():
    """memory_provider 异常不拖垮主流程（fail-soft）。"""
    store = make_store()

    async def bad_provider(session):
        raise RuntimeError("memory backend down")

    context = ContextBuilder(
        counter=TokenCounter(),
        config=BudgetConfig(),
        memory_provider=bad_provider)

    session = Session(session_id="s1", messages=[
        Message(role="user", content="hello"),
    ])
    messages = await context.build(session, tools=None)
    assert len(messages) >= 1          # 主流程不受影响
    store.close()


async def test_preference_persists_across_sessions():
    """验收 Demo：会话 A 抽取约定 → 会话 B 召回注入。

    会话 1 告诉 Agent"信号回调命名 _on_x_y" → 抽取 →
    会话 2 让它"加按钮信号" → 召回注入该约定。
    """
    store = make_store()

    # ---- 会话 A：抽取 ----
    extract_result = json.dumps([
        {"kind": "semantic", "content": "信号回调命名 _on_x_y",
         "importance": 0.9, "source": "user_stated"},
    ])
    decide_result = json.dumps({"action": "ADD"})
    llm = FakeExtractLLM([extract_result, decide_result])
    extractor = MemoryExtractor(llm, fake_embed, store, project_id="proj-a")

    session_a = Session(session_id="sess-a", messages=make_session_messages())
    report = await extractor.extract_from_session(session_a, project_id="proj-a")
    assert report.added == 1

    # ---- 会话 B：召回 ----
    retriever = MemoryRetriever(store, config=RecallConfig(k=5, max_tokens=2000))
    scored = await retriever.recall("proj-a", "给按钮加信号回调")
    assert len(scored) >= 1
    assert "_on_x_y" in scored[0].record.content
    rendered = retriever.render(scored)
    assert "<project_memory>" in rendered
    assert "_on_x_y" in rendered
    store.close()


async def test_context_builder_without_memory_provider_unchanged():
    """无 memory_provider 时 ContextBuilder 行为不变（向后兼容）。"""
    context = ContextBuilder(counter=TokenCounter(), config=BudgetConfig())
    session = Session(session_id="s1", messages=[
        Message(role="system", content="system"),
        Message(role="user", content="hello"),
    ])
    messages = await context.build(session, tools=None)
    # 无 memory 分区内容（保持原行为）
    assert all("<project_memory>" not in (m.content or "") for m in messages)


async def test_episodic_memory_recalled_for_similar_task():
    """验收 Demo：上次踩坑经验在复现场景被召回（情景记忆命中）。"""
    store = make_store()
    from agent_godot.memory import MemoryRecord
    await store.add(MemoryRecord.make(
        "episodic",
        "Godot 4.3 的 CharacterBody2D contacts_reported 改名 max_contacts_reported",
        project_id="proj-a", importance=0.6))

    retriever = MemoryRetriever(store, config=RecallConfig(k=5, max_tokens=2000))
    # 新会话做类似任务 → 应召回上次踩坑
    scored = await retriever.recall("proj-a", "给敌人加碰撞检测")
    assert len(scored) >= 1
    assert "max_contacts_reported" in scored[0].record.content
    store.close()
