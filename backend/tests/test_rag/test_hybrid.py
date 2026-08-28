"""M10 §5 验收②：混合检索在 API 名查询上显著优于单路 + RRF 手算对拍。"""
import pytest

from agent_godot.rag import Chunk, rrf_fuse


# ---------------------------------------------------------------- RRF 数学

def test_rrf_hand_computed_case():
    """§1.4 手算对拍：vec=[A,B,C,D] bm25=[B,E,A,F] → B、A 靠前。"""
    fused = rrf_fuse([["A", "B", "C", "D"], ["B", "E", "A", "F"]])
    order = [d for d, _ in fused]
    assert order[:2] == ["B", "A"]             # 双票共识登顶
    assert order[2:4] in (["E", "C"], ["C", "E"])   # 单边票
    # 精确分数
    scores = dict(fused)
    assert scores["B"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["A"] == pytest.approx(1 / 61 + 1 / 63)


def test_rrf_empty_and_single_route():
    assert rrf_fuse([]) == []
    assert rrf_fuse([["x", "y"]]) == [("x", 1 / 61), ("y", 1 / 62)]


def test_rrf_rank_starts_at_one():
    fused = rrf_fuse([["only"]], k=60)
    assert fused[0] == ("only", pytest.approx(1 / 61))


# ---------------------------------------------------------------- 混合检索

class MapEmbedder:
    """受控嵌入：文本 → 预设向量（构造'向量路瞎区'场景）。"""

    def __init__(self, mapping: dict[str, list[float]], default: list[float]):
        self.mapping = mapping
        self.default = default

    def _get(self, text: str) -> list[float]:
        return self.mapping.get(text, self.default)

    async def embed_documents(self, texts):
        return [self._get(t) for t in texts]

    async def embed_query(self, q):
        return self._get(q)


def _e(dim: int, i: int) -> list[float]:
    v = [0.0] * dim
    v[i] = 1.0
    return v


async def test_hybrid_beats_single_route_on_api_names(bm25, vec):
    """§5 验收：查"max_contacts_reported"——混合命中，纯向量不中。

    场景：语义块 S 与查询向量同向（向量路第一）；API 块 X 与查询正交
    （向量路瞎区）但含精确术语（BM25 第一）——RRF 让两路各补对方瞎区。
    """
    dim = 8
    s = Chunk(text="怎么让角色碰墙不掉下去：用 move_and_slide 后读碰撞信息",
              source="physics.md", heading="角色物理", start=1,
              doc_id="d", kind="md", seq=0)
    x = Chunk(text="max_contacts_reported 属性控制每帧报告的最大碰撞数，"
                   "默认值从 4 提升到 8",
              source="physics.md", heading="碰撞报告", start=20,
              doc_id="d", kind="md", seq=1)
    filler = [Chunk(text=f"无关干扰内容 {i}" * 5, source="f.md", heading="",
                    start=i, doc_id="f", kind="md", seq=i) for i in range(8)]

    query = "max_contacts_reported 4.3 变更"
    embedder = MapEmbedder(
        mapping={s.text: _e(dim, 0), x.text: [-1.0] + [0.0] * (dim - 1),  # X 与查询负相关
                 query: _e(dim, 0)},
        default=_e(dim, 7))
    # S 与查询同向（向量路第一）；X 负相关（向量路垫底）；filler 正交

    all_chunks = [s, x] + filler
    vec.upsert(all_chunks, await embedder.embed_documents(
        [c.text for c in all_chunks]))
    bm25.build(all_chunks)

    # 纯向量 top5：X 不在（正交瞎区）
    q_emb = await embedder.embed_query(query)
    vec_hits = [c.chunk_id for c, _ in await vec.search(q_emb, top=5)]
    assert x.chunk_id not in vec_hits
    # BM25 路：X 第一
    assert bm25.search(query, top=3)[0][0] == x.chunk_id

    # 混合：X 靠 BM25 单边票浮上来（§5 验收②——向量路瞎区被补）
    from agent_godot.rag import HybridRetriever
    hybrid = HybridRetriever(vec, bm25, embedder, top_per_route=10)
    hits = await hybrid.retrieve(query, top_k=5)
    ids = [h.chunk.chunk_id for h in hits]
    assert x.chunk_id in ids
    x_hit = next(h for h in hits if h.chunk.chunk_id == x.chunk_id)
    assert "bm25" in x_hit.from_                # X 靠 BM25 路浮上来
    assert s.text in {h.chunk.text for h in hits}       # 语义块仍在场


async def test_hybrid_marks_dual_route_hits(bm25, vec, retriever, embedder, md_doc):
    """双路命中 → from_ == {"vec", "bm25"}。"""
    from agent_godot.rag import StructureAwareChunker
    chunks = StructureAwareChunker().split(md_doc)
    vec.upsert(chunks, await embedder.embed_documents([c.text for c in chunks]))
    bm25.build(chunks)
    hits = await retriever.retrieve("move_and_slide 返回值碰撞", top_k=5)
    assert hits
    dual = [h for h in hits if len(h.from_) == 2]
    assert dual, "双路共识文档应存在且 from_ 标记两路"


async def test_hybrid_degrades_to_bm25_when_embedding_down(bm25, vec, embedder):
    """向量路挂（嵌入服务异常）→ fail-soft 单路 BM25，不崩。"""

    class BrokenEmbedder:
        async def embed_documents(self, texts):
            raise RuntimeError("embedding service down")

        async def embed_query(self, q):
            raise RuntimeError("embedding service down")

    c = Chunk(text="move_and_slide 文档", source="a.md", heading="",
              start=1, doc_id="d", kind="md", seq=0)
    bm25.build([c])
    from agent_godot.rag import HybridRetriever
    hybrid = HybridRetriever(vec, bm25, BrokenEmbedder())
    hits = await hybrid.retrieve("move_and_slide", top_k=3)
    assert [h.chunk.chunk_id for h in hits] == ["d:0"]
    assert hits[0].from_ == {"bm25"}
