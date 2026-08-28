"""rag/retrieval/vector_index.py —— Milvus 封装 + 内存版（M10 §1.2 / §4 步骤 5）

VectorIndex（生产路）：Milvus collection 封装——选 Milvus 的实际理由是
doc_id 删除（用户删文档要能删向量，FAISS 做不到，§1.2 演进）。
HNSW：跳表式分层图，M=16（每节点连接数）/ efConstruction=200（建图质量）。

InMemoryVectorIndex（单机路）：内存暴力扫——M08 MemoryStore 同思路，
<10k 量级够用；接口与 VectorIndex 完全同形，测试/无 Milvus 环境即插即用
（Windows 无 Milvus Lite 时的本机开发形态）。

铁律：
- uri 部署形态早期定死：本地文件（Lite）与 http（Standalone 容器）数据不互通
- dim 必须与嵌入模型输出一致（bge-m3=1024）；换嵌入模型 = 全量重建库
"""
from __future__ import annotations

import asyncio
import logging
import math

from ..chunking import Chunk

logger = logging.getLogger(__name__)

_FIELDS = ("doc_id", "kind", "source", "heading", "start", "text")


class VectorIndex:
    """Milvus collection 封装（chunk_id 字符串主键，upsert 天然幂等覆盖）。

    uri 约定：本地文件路径（Milvus Lite，macOS/Linux）或
    http://host:19530（standalone 容器）。
    """

    def __init__(self, collection: str = "godot_kb", uri: str = "milvus_demo.db",
                 dim: int = 1024):
        try:
            from pymilvus import MilvusClient    # 懒 import：不用 Milvus 的环境零开销
        except ImportError as e:
            raise ImportError("VectorIndex 需要 pymilvus：uv add pymilvus") from e
        self.collection = collection
        self.dim = dim
        self.client = MilvusClient(uri=uri)
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        from pymilvus import DataType
        if self.client.has_collection(self.collection):
            self.client.load(self.collection)
            return
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=128)
        schema.add_field("kind", DataType.VARCHAR, max_length=32)
        schema.add_field("source", DataType.VARCHAR, max_length=768)
        schema.add_field("heading", DataType.VARCHAR, max_length=768)
        schema.add_field("start", DataType.INT64)
        schema.add_field("text", DataType.VARCHAR, max_length=65535)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dim)
        index_params = self.client.prepare_index_params()
        index_params.add_index(
            field_name="vector", index_type="HNSW",
            metric_type="IP",                      # 归一化后 IP == 余弦
            params={"M": 16, "efConstruction": 200})
        self.client.create_collection(self.collection, schema=schema,
                                      index_params=index_params)
        logger.info("Milvus collection %s 已创建（dim=%s, HNSW M=16）",
                    self.collection, self.dim)

    # ---------------- 写路径 ----------------

    def upsert(self, chunks: list[Chunk], embs: list[list[float]]) -> int:
        """批量写入（分批防单请求超限）。返回写入条数。"""
        if len(chunks) != len(embs):
            raise ValueError(f"chunks({len(chunks)}) 与 embs({len(embs)}) 数量不一致")
        rows = [{
            "chunk_id": c.chunk_id, "doc_id": c.doc_id, "kind": c.kind,
            "source": c.source[:768], "heading": c.heading[:768],
            "start": c.start, "text": c.text[:65535], "vector": e,
        } for c, e in zip(chunks, embs)]
        for i in range(0, len(rows), 256):
            self.client.upsert(self.collection, rows[i:i + 256])
        return len(rows)

    def delete_doc(self, doc_id: str) -> int:
        """expr 过滤删：增量更新"向量先删"的执行点（§3 一致性顺序）。"""
        n = self.count(doc_id)
        self.client.delete(self.collection, filter=f'doc_id == "{doc_id}"')
        return n

    # ---------------- 读路径 ----------------

    async def search(self, q_emb: list[float], top: int = 10,
                     filter_kind: str | None = None
                     ) -> list[tuple[Chunk, float]]:
        """ANN 检索（IP 度量 + 可选 kind 标量过滤），同步 client 包 to_thread。"""
        expr = f'kind == "{filter_kind}"' if filter_kind else ""
        res = await asyncio.to_thread(
            self.client.search, self.collection, data=[q_emb], limit=top,
            filter=expr, output_fields=list(_FIELDS),
            search_params={"metric_type": "IP", "params": {"ef": 64}})
        hits: list[tuple[Chunk, float]] = []
        for hit in res[0]:
            e = hit["entity"]
            hits.append((Chunk(
                text=e["text"], source=e["source"], heading=e["heading"],
                start=e["start"], doc_id=e["doc_id"], kind=e["kind"],
            ), hit["distance"]))
        return hits

    def count(self, doc_id: str | None = None) -> int:
        expr = f'doc_id == "{doc_id}"' if doc_id else 'chunk_id != ""'
        rows = self.client.query(self.collection, filter=expr,
                                 output_fields=["chunk_id"], limit=16384)
        return len(rows)


class InMemoryVectorIndex:
    """内存暴力扫版（M08 MemoryStore 同思路）：接口与 VectorIndex 同形。"""

    def __init__(self, dim: int | None = None):
        self._store: dict[str, tuple[Chunk, list[float]]] = {}
        self._locked_dim = dim          # 维度守卫：随首个条目锁定

    def _guard_dim(self, emb: list[float]) -> bool:
        """换嵌入模型守卫：维度漂移 → 拒收（M10 铁律的入口执行）。"""
        if self._locked_dim is None:
            self._locked_dim = len(emb)
            return True
        if len(emb) != self._locked_dim:
            logger.warning("嵌入维度漂移 %s→%s，该批拒收（换模型=重建库）",
                           self._locked_dim, len(emb))
            return False
        return True

    def upsert(self, chunks: list[Chunk], embs: list[list[float]]) -> int:
        if len(chunks) != len(embs):
            raise ValueError(f"chunks({len(chunks)}) 与 embs({len(embs)}) 数量不一致")
        n = 0
        for c, e in zip(chunks, embs):
            if e and self._guard_dim(e):
                self._store[c.chunk_id] = (c, e)     # 同 id 覆盖（幂等）
                n += 1
        return n

    def delete_doc(self, doc_id: str) -> int:
        dead = [cid for cid, (c, _) in self._store.items() if c.doc_id == doc_id]
        for cid in dead:
            del self._store[cid]
        return len(dead)

    async def search(self, q_emb: list[float], top: int = 10,
                     filter_kind: str | None = None
                     ) -> list[tuple[Chunk, float]]:
        if not q_emb or not self._store:
            return []
        scored: list[tuple[Chunk, float]] = []
        for c, e in self._store.values():
            if filter_kind and c.kind != filter_kind:
                continue
            if len(e) != len(q_emb):        # 维度不符直接跳过（脏数据不进榜）
                continue
            scored.append((c, _cos(q_emb, e)))
        scored.sort(key=lambda x: (-x[1], x[0].chunk_id))
        return scored[:top]

    def count(self, doc_id: str | None = None) -> int:
        if doc_id is None:
            return len(self._store)
        return sum(1 for c, _ in self._store.values() if c.doc_id == doc_id)


def _cos(a: list[float], b: list[float]) -> float:
    """余弦（不依赖输入已归一化——假嵌入等来源未归一时仍正确）。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


__all__ = ["InMemoryVectorIndex", "VectorIndex"]
