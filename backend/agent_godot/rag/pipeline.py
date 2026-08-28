"""rag/pipeline.py —— 建库流水线 + 三存储一致性（M10 §3 / §4 步骤 8）

全量重建简单，增量才是生产题：用户改了 1 个文档，只重建它的 chunks 且删旧向量。

三个存储（Milvus 向量 / BM25 稀疏 / SQLite 元数据）的一致性：
操作顺序设计成"最坏可恢复"——先删旧（向量→稀疏）再插新、元数据最后原子替换：
中途崩了 = 该文档在新旧之间"多删"（检索不到但不脏），重建一次即愈；
绝不会出现"旧向量残留 + 新元数据"的脏命中。辅以 audit() 对账：
定期比对三处 doc_id→chunk 计数，不一致告警 + 触发重建。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from .chunking import Chunk, Chunker, StructureAwareChunker
from .embedding import EmbeddingService
from .parsers import ParsedDoc, get_parser
from .retrieval import BM25Index, InMemoryVectorIndex

logger = logging.getLogger(__name__)


class ChunkStore:
    """三存储之一：chunk 元数据库（SQLite，M08 风格）。"""

    def __init__(self, db_path: str | Path = ":memory:"):
        self.db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                doc_id   TEXT NOT NULL,
                seq      INTEGER NOT NULL,
                text     TEXT NOT NULL,
                source   TEXT NOT NULL,
                heading  TEXT NOT NULL,
                start    INTEGER NOT NULL,
                kind     TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
            CREATE TABLE IF NOT EXISTS docs (
                doc_id     TEXT PRIMARY KEY,
                source     TEXT NOT NULL,
                kind       TEXT NOT NULL,
                updated_at REAL NOT NULL);
        """)
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._init_db()
        return self._conn  # type: ignore[return-value]

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    async def chunk_ids_of(self, doc_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT chunk_id FROM chunks WHERE doc_id=?", (doc_id,)).fetchall()
        return [r["chunk_id"] for r in rows]

    async def replace_chunks(self, doc_id: str, chunks: list[Chunk]) -> None:
        """原子替换（单事务）：DELETE 旧 + INSERT 新 + upsert docs。"""
        now = time.time()
        with self.conn:                     # 事务：要么全成要么全回滚
            self.conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            if chunks:
                self.conn.executemany(
                    """INSERT INTO chunks
                       (chunk_id, doc_id, seq, text, source, heading, start, kind)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(c.chunk_id, doc_id, c.seq, c.text, c.source, c.heading,
                      c.start, c.kind) for c in chunks])
            self.conn.execute(
                """INSERT INTO docs (doc_id, source, kind, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(doc_id) DO UPDATE SET
                     source=excluded.source, kind=excluded.kind,
                     updated_at=excluded.updated_at""",
                (doc_id, chunks[0].source if chunks else "",
                 chunks[0].kind if chunks else "", now))

    async def count(self, doc_id: str | None = None) -> int:
        if doc_id is None:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE doc_id=?",
                (doc_id,)).fetchone()
        return int(row["n"])

    async def all_doc_ids(self) -> list[str]:
        rows = self.conn.execute("SELECT doc_id FROM docs").fetchall()
        return [r["doc_id"] for r in rows]

    async def get_chunks(self, doc_id: str) -> list[Chunk]:
        rows = self.conn.execute(
            """SELECT * FROM chunks WHERE doc_id=? ORDER BY seq""",
            (doc_id,)).fetchall()
        return [Chunk(text=r["text"], source=r["source"], heading=r["heading"],
                     start=r["start"], doc_id=r["doc_id"], kind=r["kind"],
                     seq=r["seq"]) for r in rows]


class IngestPipeline:
    """离线建库流水线：解析 → 切分 → 嵌入 → 双索引 + 元数据（增量幂等）。"""

    def __init__(self, chunker: Chunker | None = None,
                 embedder: EmbeddingService | None = None,
                 vec=None, bm25: BM25Index | None = None,
                 db: ChunkStore | None = None):
        self.chunker = chunker or StructureAwareChunker()
        self.embedder = embedder or EmbeddingService()
        self.vec = vec or InMemoryVectorIndex()     # 生产：VectorIndex(uri=...)
        self.bm25 = bm25 or BM25Index()
        self.db = db or ChunkStore()

    async def upsert_document(self, doc: ParsedDoc) -> int:
        """增量灌库（§3 难点代码的忠实落地）：先删旧 → 插新 → 元数据收尾。"""
        new_chunks = self.chunker.split(doc)
        old = await self.db.chunk_ids_of(doc.doc_id)
        if old:
            self.vec.delete_doc(doc.doc_id)         # 向量先删
            self.bm25.remove(old)                   # 稀疏索引同步删
        self.bm25.build_incremental(new_chunks)     # 增量插入
        embs = await self.embedder.embed_documents([c.text for c in new_chunks])
        self.vec.upsert(new_chunks, embs)
        await self.db.replace_chunks(doc.doc_id, new_chunks)   # 元数据最后原子替换
        return len(new_chunks)

    async def ingest(self, source: str | Path, kind: str | None = None) -> int:
        """便捷入口：解析 + 灌库。source 是路径或 URL。"""
        parser = get_parser(kind, source=source)
        doc = parser.parse(source)
        return await self.upsert_document(doc)

    async def audit(self) -> dict[str, dict[str, int]]:
        """三存储对账：doc_id → {vec, bm25, meta} 计数，只报漂移项。

        不一致 = 曾有中途崩溃 → 告警 + 重建该文档（重跑 upsert 即愈）。
        """
        drift: dict[str, dict[str, int]] = {}
        for doc_id in await self.db.all_doc_ids():
            counts = {
                "vec": self.vec.count(doc_id),
                "bm25": self.bm25.count(doc_id),
                "meta": await self.db.count(doc_id),
            }
            if len(set(counts.values())) > 1:
                drift[doc_id] = counts
                logger.warning("对账漂移 doc=%s: %s（重建即愈）", doc_id, counts)
        return drift


__all__ = ["ChunkStore", "IngestPipeline"]
