"""路由决策：意图四分支 · 开关硬约束 · 多跳信号（M12 §1.3 / §5 表驱动）。"""
from __future__ import annotations

import pytest

from agent_godot.query_engine import (Channel, Intent, QueryRouter,
                                      RoutingContext, multi_hop_hint)


@pytest.fixture
def router() -> QueryRouter:
    return QueryRouter()


def test_multi_hop_hint_words():
    assert multi_hop_hint("删掉 hitbox 信号影响哪些场景")
    assert multi_hop_hint("player 和 enemy 之间的区别是什么")
    assert multi_hop_hint("这个方法被哪些脚本依赖")
    assert not multi_hop_hint("Area2D 怎么检测碰撞")
    assert not multi_hop_hint("")


def test_chitchat_empty_channels(router):
    plan = router.decide(Intent.CHITCHAT, RoutingContext())
    assert plan.channels == []
    assert plan.mode is None
    assert "零检索" in plan.reason


def test_code_edit_goes_craft(router):
    plan = router.decide(Intent.CODE_EDIT, RoutingContext())
    assert plan.channels == [] and plan.mode == "craft"
    assert "craft" in plan.reason


def test_search_web_enabled(router):
    ctx = RoutingContext(web_enabled=True)
    plan = router.decide(Intent.SEARCH, ctx)
    assert plan.channels == [Channel.WEB]
    assert plan.budget["web_n"] == 5


def test_search_web_off_degrades_to_knowledge(router):
    """联网停诊（开关硬约束）：search 意图降级知识通道并明示。"""
    ctx = RoutingContext(web_enabled=False, kb_enabled=True)
    plan = router.decide(Intent.SEARCH, ctx)
    assert Channel.WEB not in plan.channels
    assert "联网未启用" in plan.reason
    assert Channel.RAG in plan.channels


def test_router_respects_user_switches(router):
    """验收 §5：用户关了知识库 → 不走 RAG，reason 明示"未启用"（信任优先）。"""
    ctx = RoutingContext(kb_enabled=False)
    plan = router.decide(Intent.KNOWLEDGE, ctx)
    assert Channel.RAG not in plan.channels
    assert plan.channels == [Channel.LLM_DIRECT]
    assert "未启用" in plan.reason


def test_knowledge_default_channels(router):
    ctx = RoutingContext(kb_enabled=True, graph_ready=True)
    plan = router.decide(Intent.KNOWLEDGE, ctx)
    assert Channel.RAG in plan.channels and Channel.GRAPH in plan.channels
    assert plan.budget["rag_top_k"] == 3


def test_multi_hop_hint_promotes_graph_first(router):
    """多跳信号词 → GRAPH 优先（项目事实权威），RAG 补文档。"""
    ctx = RoutingContext(kb_enabled=True, graph_ready=True,
                         multi_hop_hint=True)
    plan = router.decide(Intent.KNOWLEDGE, ctx)
    assert plan.channels == [Channel.GRAPH, Channel.RAG]


def test_graph_not_ready_skipped(router):
    ctx = RoutingContext(kb_enabled=True, graph_ready=False)
    plan = router.decide(Intent.KNOWLEDGE, ctx)
    assert Channel.GRAPH not in plan.channels
    assert plan.channels == [Channel.RAG]
    assert "图谱未就绪" in plan.reason


def test_unknown_falls_back_to_knowledge(router):
    """unknown 出口路由到保守默认（knowledge）——意图集演化留的后门。"""
    ctx = RoutingContext(kb_enabled=True, graph_ready=False)
    plan = router.decide(Intent.UNKNOWN, ctx)
    assert plan.channels == [Channel.RAG]


def test_all_off_direct_with_note(router):
    ctx = RoutingContext(kb_enabled=False, web_enabled=False,
                         graph_ready=False)
    plan = router.decide(Intent.KNOWLEDGE, ctx)
    assert plan.channels == [Channel.LLM_DIRECT]
    assert "私有文档" in plan.reason


def test_plan_meta_carry_intent(router):
    plan = router.decide(Intent.CHITCHAT, RoutingContext())
    assert plan.meta["intent"] == "chitchat"
