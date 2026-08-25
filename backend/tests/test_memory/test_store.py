"""tests/test_memory/test_store.py —— M08 §5：CRUD / 检索 / 归档 / GC。"""
from __future__ import annotations

import time

from agent_godot.memory import MemoryRecord, MemoryStore, cosine, fake_embed

from .conftest import make_store


async def test_add_and_search_finds_record():
    """add 落库 → search 按余弦召回。"""
    store = make_store()
    rec = MemoryRecord.make("semantic", "用户偏好：GDScript 缩进用 tabs",
                            project_id="proj-a", importance=0.9)
    await store.add(rec)
    q_emb = fake_embed("缩进偏好")
    results = await store.search("proj-a", q_emb, top=5)
    assert len(results) == 1
    assert results[0].id == rec.id
    assert results[0].status == "active"
    store.close()


async def test_archive_hides_from_search_but_record_remains():
    """软删红线：archive 后 search 不可见，但 get_by_id 还在。"""
    store = make_store()
    rec = MemoryRecord.make("episodic", "踩坑：Godot 4.3 改名",
                            project_id="proj-a")
    await store.add(rec)
    await store.archive(rec.id, reason="矛盾归档")

    q_emb = fake_embed("踩坑")
    assert await store.search("proj-a", q_emb) == []

    # 记录还在（可审计可恢复）
    row = await store.get_by_id(rec.id)
    assert row is not None
    assert row.status == "archived"
    store.close()


async def test_project_isolation():
    """召回按项目隔离——A 项目约定不污染 B 项目。"""
    store = make_store()
    await store.add(MemoryRecord.make(
        "semantic", "A 项目用 tabs", project_id="proj-a"))
    await store.add(MemoryRecord.make(
        "semantic", "B 项目用 spaces", project_id="proj-b"))

    q_emb = fake_embed("缩进偏好")
    a_results = await store.search("proj-a", q_emb)
    b_results = await store.search("proj-b", q_emb)
    assert all(r.project_id == "proj-a" for r in a_results)
    assert all(r.project_id == "proj-b" for r in b_results)
    store.close()


async def test_update_recomputes_embedding():
    """update 重算 emb + 改 content。"""
    store = make_store()
    rec = MemoryRecord.make("semantic", "缩进用 spaces", project_id="proj-a")
    await store.add(rec)
    await store.update(rec.id, "缩进用 tabs")
    row = await store.get_by_id(rec.id)
    assert row.content == "缩进用 tabs"
    # 搜 tabs 能命中（emb 已更新）
    q_emb = fake_embed("缩进用 tabs")
    results = await store.search("proj-a", q_emb)
    assert any(r.id == rec.id for r in results)
    store.close()


async def test_decay_days_defaults_by_kind():
    """episodic=14 / semantic=90 的默认衰减天数。"""
    assert MemoryRecord.make("episodic", "x", "p").decay_days == 14
    assert MemoryRecord.make("semantic", "x", "p").decay_days == 90


async def test_clusters_finds_similar():
    """clusters 检测相似簇（GC 用）。"""
    store = make_store()
    # fake_embed 是哈希伪随机——同内容才相似，不同内容随机方向
    await store.add(MemoryRecord.make(
        "semantic", "缩进用 tabs", project_id="p", importance=0.9))
    await store.add(MemoryRecord.make(
        "semantic", "缩进用 tabs", project_id="p", importance=0.5))  # 同内容→同 emb
    await store.add(MemoryRecord.make(
        "semantic", "完全不同的事", project_id="p"))
    clusters = await store.clusters(sim_threshold=0.99, project_id="p")
    assert len(clusters) == 1
    assert len(clusters[0]) == 2     # 两条"缩进用 tabs"成簇
    store.close()


async def test_gc_merge_duplicates():
    """GC 合并：保留新且重要者，其余归档。"""
    store = make_store()
    old_ts = time.time() - 86400
    await store.add(MemoryRecord.make(
        "semantic", "缩进用 tabs", project_id="p",
        importance=0.5, ts=old_ts))
    await store.add(MemoryRecord.make(
        "semantic", "缩进用 tabs", project_id="p",
        importance=0.9, ts=time.time()))
    archived = await store.gc_merge_duplicates(sim_threshold=0.99, project_id="p")
    assert archived == 1
    active = await store.get_active("p")
    assert len(active) == 1
    assert active[0].importance == 0.9     # 保留了重要的
    store.close()


def test_cosine_basic():
    """余弦相似度基本正确性。"""
    assert abs(cosine([1, 0], [1, 0]) - 1.0) < 1e-6
    assert abs(cosine([1, 0], [0, 1]) - 0.0) < 1e-6
    assert cosine([], []) == 0.0


async def test_profile_data_roundtrip():
    """Profile 数据存取往返。"""
    store = make_store()
    await store.save_profile_data("proj-a", {"godot_version": "4.3",
                                             "naming_conventions": {"indent": "tabs"}})
    data = await store.get_profile_data("proj-a")
    assert data["godot_version"] == "4.3"
    assert data["naming_conventions"]["indent"] == "tabs"
    store.close()
