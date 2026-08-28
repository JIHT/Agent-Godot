"""M10 §4 步骤 5：InMemoryVectorIndex（Milvus 同形接口的内存版）。"""
import pytest

from agent_godot.rag import Chunk


def _chunk(seq: int, kind: str = "md") -> Chunk:
    return Chunk(text=f"text {seq}", source="a.md", heading="h",
                 start=1, doc_id="d1", kind=kind, seq=seq)


def _unit(dim: int, i: int, jitter: float = 0.0) -> list[float]:
    v = [jitter * (j + 1) / dim for j in range(dim)]
    v[i] = 1.0
    return v


async def test_search_orders_by_cosine(vec):
    dim = 8
    chunks = [_chunk(0), _chunk(1), _chunk(2)]
    vec.upsert(chunks, [_unit(dim, 0), _unit(dim, 1), _unit(dim, 2)])
    hits = await vec.search(_unit(dim, 1), top=3)
    assert hits[0][0].chunk_id == "d1:1"          # 与查询同向者第一
    assert {c.chunk_id for c, _ in hits} == {"d1:0", "d1:1", "d1:2"}


async def test_search_top_limit(vec):
    dim = 8
    vec.upsert([_chunk(i) for i in range(5)],
               [_unit(dim, i % 8) for i in range(5)])
    hits = await vec.search(_unit(dim, 0), top=2)
    assert len(hits) == 2


async def test_search_filter_kind(vec):
    dim = 8
    chunks = [_chunk(0, "md"), _chunk(1, "gdscript")]
    vec.upsert(chunks, [_unit(dim, 0), _unit(dim, 1)])
    hits = await vec.search(_unit(dim, 1), top=10, filter_kind="gdscript")
    assert [c.chunk_id for c, _ in hits] == ["d1:1"]


def test_delete_doc(vec):
    dim = 8
    vec.upsert([_chunk(0), _chunk(1)], [_unit(dim, 0), _unit(dim, 1)])
    n = vec.delete_doc("d1")
    assert n == 2
    assert vec.count() == 0
    assert vec.count("d1") == 0


def test_upsert_idempotent(vec):
    dim = 8
    c = _chunk(0)
    vec.upsert([c], [_unit(dim, 0)])
    vec.upsert([c], [_unit(dim, 0)])          # 同 chunk_id 覆盖
    assert vec.count() == 1


def test_dimension_guard_rejects_drift(vec):
    """换嵌入模型 = 维度漂移 → 拒收（M10 铁律的入口执行）。"""
    c = _chunk(0)
    assert vec.upsert([c], [_unit(8, 0)]) == 1
    assert vec.upsert([_chunk(1)], [_unit(64, 0)]) == 0    # 64 维被拒
    assert vec.count() == 1


def test_upsert_length_mismatch_raises(vec):
    with pytest.raises(ValueError):
        vec.upsert([_chunk(0)], [])
