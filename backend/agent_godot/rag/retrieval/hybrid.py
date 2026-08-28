"""rag/retrieval/hybrid.py —— 两路检索 + RRF 融合（M10 §1.4 / §4 步骤 6）

馆员双路找书：向量路按"意思像不像"（embed_query → ANN），
BM25 路按"术语命中"（精确 token 匹配）——两路各补对方瞎区：
语义改写型查询走向量，API 名查询走 BM25。

RRF 只看名次不融原始分（余弦 0~1 vs BM25 0~25，量纲不可比）；
两路 doc_id 同构（同一 chunk 同一主键）是 RRF 生效的前提。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..chunking import Chunk

logger = logging.getLogger(__name__)


@dataclass
class RetrievalHit:
    """一条检索命中：chunk + RRF 融合分 + 命中来源。"""
    chunk: Chunk
    score: float                    # RRF 融合分（rerank 后被替换为交叉编码分）
    from_: set[str] = field(default_factory=set)    # {"vec", "bm25"}


def rrf_fuse(rankings: list[list[str]], k: int = 60, top_k: int = 10
             ) -> list[tuple[str, float]]:
    """RRF：各路倒数名次累加。k=60 原论文经验值（平滑名次差防第 1 名独裁）。"""
    scores: dict[str, float] = {}
    for ranking in rankings:                        # 每路一个有序 id 列表
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])[:top_k]


class HybridRetriever:
    """两路检索（top_per_route 同预算防偏科）→ RRF → RetrievalHit。"""

    def __init__(self, vec, bm25, embedder, k_rrf: int = 60,
                 top_per_route: int = 50):
        self.vec = vec               # VectorIndex / InMemoryVectorIndex（同形）
        self.bm25 = bm25             # BM25Index
        self.embedder = embedder     # EmbeddingService / FakeEmbeddingService
        self.k_rrf = k_rrf
        self.top_per_route = top_per_route

    async def retrieve(self, query: str, top_k: int = 10) -> list[RetrievalHit]:
        """混合检索主路径。"""
        # ---- 向量路（fail-soft：嵌入服务挂 → 单路 BM25 半开卷）----
        vec_hits: list[tuple[Chunk, float]] = []
        try:
            q_emb = await self.embedder.embed_query(query)
            if q_emb:
                vec_hits = await self.vec.search(q_emb, top=self.top_per_route)
        except Exception as e:                       # noqa: BLE001 —— 检索降级不崩
            logger.warning("向量路不可用，仅 BM25 单路: %s", e)

        # ---- BM25 路 ----
        bm25_hits = self.bm25.search(query, top=self.top_per_route)

        # ---- RRF 融合 + 回填 ----
        vec_rank = [c.chunk_id for c, _ in vec_hits]
        bm25_rank = [cid for cid, _ in bm25_hits]
        fused = rrf_fuse([vec_rank, bm25_rank], k=self.k_rrf, top_k=top_k)

        chunk_of = {c.chunk_id: c for c, _ in vec_hits}
        vec_set, bm25_set = set(vec_rank), set(bm25_rank)
        hits: list[RetrievalHit] = []
        for cid, score in fused:
            chunk = chunk_of.get(cid) or self.bm25.get_chunk(cid)
            if chunk is None:            # 理论不可达：两路之一必持有 chunk
                continue
            from_ = set()
            if cid in vec_set:
                from_.add("vec")
            if cid in bm25_set:
                from_.add("bm25")
            hits.append(RetrievalHit(chunk=chunk, score=score, from_=from_))
        return hits


__all__ = ["HybridRetriever", "RetrievalHit", "rrf_fuse"]
