"""tests/test_memory/test_retriever.py —— M08 §5 / §1.3：三因子加权召回。"""
from __future__ import annotations

import math
import time

from agent_godot.memory import (MemoryRecord, MemoryRetriever, RecallConfig,
                                MemoryStore, fake_embed)

from .conftest import make_store


async def test_recall_returns_scored_results():
    """recall 返回 ScoredMemory 列表，按分排序。"""
    store = make_store()
    await store.add(MemoryRecord.make(
        "semantic", "用户偏好：GDScript 缩进用 tabs",
        project_id="p", importance=0.9))
    retriever = MemoryRetriever(store, config=RecallConfig(k=5, max_tokens=5000))
    scored = await retriever.recall("p", "缩进偏好是什么")
    assert len(scored) >= 1
    assert scored[0].record.kind == "semantic"
    store.close()


async def test_recall_prefers_recent_same_similarity():
    """同相似度下：7 天前的记忆 > 60 天前（时间衰减拉开差距）。

    代数字见 §1.3：A(7天) score=0.69 > B(60天) score=0.57
    """
    store = make_store()
    now = time.time()
    # 同内容→同 emb→同相似度；不同 ts→不同新鲜度
    await store.add(MemoryRecord.make(
        "episodic", "敌人巡逻行为踩坑", project_id="p",
        importance=0.6, ts=now - 7 * 86400))
    await store.add(MemoryRecord.make(
        "episodic", "敌人巡逻行为踩坑", project_id="p",
        importance=0.6, ts=now - 60 * 86400))
    retriever = MemoryRetriever(store, config=RecallConfig(k=5, max_tokens=5000))
    scored = await retriever.recall("p", "敌人巡逻行为踩坑")
    assert len(scored) == 2
    # 7 天前的应该排前
    assert scored[0].record.ts > scored[1].record.ts
    store.close()


async def test_recall_prefers_important():
    """同相似度同时间下：importance 0.9 > 0.5。"""
    store = make_store()
    now = time.time()
    await store.add(MemoryRecord.make(
        "semantic", "命名约定 _on_x_y", project_id="p",
        importance=0.5, ts=now))
    await store.add(MemoryRecord.make(
        "semantic", "命名约定 _on_x_y", project_id="p",
        importance=0.9, ts=now))
    retriever = MemoryRetriever(store, config=RecallConfig(k=5, max_tokens=5000))
    scored = await retriever.recall("p", "命名约定 _on_x_y")
    assert scored[0].record.importance == 0.9
    store.close()


async def test_recall_respects_token_budget():
    """token 预算截断：超预算时截断。"""
    store = make_store()
    for i in range(5):
        await store.add(MemoryRecord.make(
            "semantic", f"约定 {i}：" + "x" * 200,  # 每条约 50 token
            project_id="p", importance=0.9))
    retriever = MemoryRetriever(store, config=RecallConfig(k=8, max_tokens=100))
    scored = await retriever.recall("p", "约定")
    assert len(scored) < 5     # 被预算截断
    store.close()


async def test_render_format():
    """渲染：XML 标签包裹 + 时间戳 + kind 标注。"""
    store = make_store()
    await store.add(MemoryRecord.make(
        "semantic", "缩进用 tabs", project_id="p", importance=0.9))
    retriever = MemoryRetriever(store, config=RecallConfig(k=5, max_tokens=5000))
    scored = await retriever.recall("p", "缩进")
    rendered = retriever.render(scored)
    assert "<project_memory>" in rendered
    assert "</project_memory>" in rendered
    assert "以工具实时读取为准" in rendered
    assert "[semantic]" in rendered
    store.close()


async def test_render_empty_returns_empty_string():
    """无记忆时渲染返回空串。"""
    store = make_store()
    retriever = MemoryRetriever(store)
    assert retriever.render([]) == ""


async def test_recall_project_isolation():
    """召回按项目隔离。"""
    store = make_store()
    await store.add(MemoryRecord.make(
        "semantic", "A 项目约定", project_id="proj-a"))
    retriever = MemoryRetriever(store)
    # 查 proj-b 不应有结果
    scored = await retriever.recall("proj-b", "约定")
    assert scored == []
    store.close()


async def test_recall_formula_matches_hand_calc():
    """验证 §1.3 代数字：score = 0.6*sim + 0.2*exp(-days/tau) + 0.2*imp"""
    store = make_store()
    now = time.time()
    rec = MemoryRecord.make(
        "episodic", "敌人巡逻行为踩坑", project_id="p",
        importance=0.6, ts=now - 7 * 86400)
    await store.add(rec)
    cfg = RecallConfig(k=5, w_sim=0.6, w_rec=0.2, w_imp=0.2, max_tokens=5000)
    retriever = MemoryRetriever(store, config=cfg)
    scored = await retriever.recall("p", "敌人巡逻行为踩坑")
    assert len(scored) == 1
    s = scored[0]
    # 手算验证
    expected_sim = s.sim       # fake_embed 的余弦
    expected_rec = math.exp(-7 / 14)
    expected_score = 0.6 * expected_sim + 0.2 * expected_rec + 0.2 * 0.6
    assert abs(s.score - expected_score) < 1e-4
    store.close()
