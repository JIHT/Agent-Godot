"""memory/store.py —— 记忆库：SQLite 持久化 + 内存向量检索（M08 §4 步骤 1）

单机版暴力扫：所有 active 记录按 project_id 取回，内存算余弦相似度。
量级 <10k 完全够用；Milvus 迁移留 M10。

软删红线：archive 只改 status='archived'，记录永不物理删除（审计/可恢复）。
维度守卫：换嵌入模型时旧向量与新查询维度不符——search 入口校验，漂移即跳过。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

logger = logging.getLogger(__name__)

# Embedder：text -> vector | None
# 与 core.semantic_cache._fake_embed / OllamaEmbedder 同签名，插槽即插即用
Embedder = Callable[[str], "list[float] | None"]


def fake_embed(text: str, dim: int = 64) -> list[float]:
    """假嵌入：文本哈希→种子→伪随机单位向量。确定性、零依赖。CI/单测专用。

    语义上不准（换说法永不命中），但同句自查可命中——足够跑通 CRUD/检索逻辑。
    """
    state = int.from_bytes(hashlib.md5(text.encode()).digest()[:8], "big")
    vec: list[float] = []
    for _ in range(dim):
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        vec.append(((state >> 33) / (1 << 31)) - 1.0)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（自带归一化，不依赖输入已归一化）。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass
class MemoryRecord:
    """一条记忆。kind 决定 decay_days（episodic=14 / semantic=90）。"""
    id: str
    kind: Literal["episodic", "semantic"]
    content: str
    emb: list[float] | None
    project_id: str
    session_id: str | None = None
    ts: float = field(default_factory=time.time)
    importance: float = 0.5
    decay_days: int = 14            # episodic 14 / semantic 90
    source: Literal["user_stated", "model_inferred"] = "user_stated"
    status: Literal["active", "archived"] = "active"

    @staticmethod
    def make(kind: Literal["episodic", "semantic"], content: str,
             project_id: str, **kw) -> MemoryRecord:
        """工厂：自动填 id / decay_days / ts，减少调用方样板。"""
        decay = 14 if kind == "episodic" else 90
        return MemoryRecord(
            id=kw.pop("id", uuid.uuid4().hex[:12]),
            kind=kind, content=content,
            emb=kw.pop("emb", None),
            project_id=project_id,
            session_id=kw.pop("session_id", None),
            ts=kw.pop("ts", time.time()),
            importance=kw.pop("importance", 0.5),
            decay_days=kw.pop("decay_days", decay),
            source=kw.pop("source", "user_stated"),
            status=kw.pop("status", "active"),
        )


class MemoryStore:
    """SQLite 持久化 + 内存向量检索（单机版暴力扫）。"""

    def __init__(self, db_path: str | Path = ":memory:",
                 embedder: Embedder | None = None):
        self.db_path = str(db_path)
        self.embedder: Embedder = embedder or fake_embed
        self._conn: sqlite3.Connection | None = None
        self._locked_dim: int | None = None    # 维度守卫：随首个条目锁定
        self._init_db()

    # ---------- 生命周期 ----------

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id              TEXT PRIMARY KEY,
                kind            TEXT NOT NULL,
                content         TEXT NOT NULL,
                emb             TEXT NOT NULL,
                project_id      TEXT NOT NULL,
                session_id      TEXT,
                ts              REAL NOT NULL,
                importance      REAL NOT NULL,
                decay_days      INTEGER NOT NULL,
                source          TEXT NOT NULL,
                status          TEXT NOT NULL,
                archived_reason TEXT,
                archived_at     REAL,
                updated_at      REAL,
                created_at      REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mem_proj_status
                ON memories(project_id, status);
            CREATE TABLE IF NOT EXISTS profiles (
                project_id  TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                updated_at  REAL NOT NULL
            );
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

    # ---------- 维度守卫 ----------

    def _guard_dim(self, vec: list[float]) -> bool:
        """换嵌入模型守卫：维度漂移 → 该条跳过（不污染检索）。

        与 semantic_cache 同理：_cos 的 zip 按短边静默截断——
        64 维旧条目对 1024 维新查询不报错但相似度全错。
        """
        if self._locked_dim is None:
            self._locked_dim = len(vec)
            return True
        if len(vec) != self._locked_dim:
            logger.warning("嵌入维度漂移 %s→%s，该条跳过检索",
                           self._locked_dim, len(vec))
            return False
        return True

    # ---------- CRUD ----------

    async def add(self, rec: MemoryRecord) -> None:
        """落库；emb 为空时自动算。"""
        if rec.emb is None:
            rec.emb = self.embedder(rec.content)
        if rec.emb:
            self._guard_dim(rec.emb)
        self.conn.execute(
            """INSERT INTO memories
               (id, kind, content, emb, project_id, session_id,
                ts, importance, decay_days, source, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rec.id, rec.kind, rec.content, json.dumps(rec.emb or []),
             rec.project_id, rec.session_id, rec.ts, rec.importance,
             rec.decay_days, rec.source, rec.status, time.time()))
        self.conn.commit()

    async def update(self, id: str, content: str) -> None:
        """更新内容并重算 emb（UPDATE 决策用）。"""
        emb = self.embedder(content)
        if emb:
            self._guard_dim(emb)
        self.conn.execute(
            """UPDATE memories SET content=?, emb=?, updated_at=?
               WHERE id=?""",
            (content, json.dumps(emb or []), time.time(), id))
        self.conn.commit()

    async def archive(self, id: str, reason: str) -> None:
        """软删（红线）：只改 status='archived'，记录保留可审计可恢复。"""
        self.conn.execute(
            """UPDATE memories SET status='archived',
               archived_reason=?, archived_at=? WHERE id=?""",
            (reason, time.time(), id))
        self.conn.commit()

    # ---------- 检索 ----------

    async def search(self, project_id: str, q_emb: list[float],
                     top: int = 32) -> list[MemoryRecord]:
        """粗筛：project_id 过滤 + 余弦 top-N（精排在 retriever 做）。"""
        if not q_emb:
            return []
        rows = self.conn.execute(
            """SELECT * FROM memories
               WHERE project_id=? AND status='active'""",
            (project_id,)).fetchall()
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            emb = json.loads(row["emb"])
            if not emb or not self._guard_dim(emb):
                continue
            sim = cosine(q_emb, emb)
            scored.append((sim, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [self._row_to_rec(r) for _, r in scored[:top]]

    async def search_by_text(self, project_id: str, text: str,
                             top: int = 32) -> list[MemoryRecord]:
        """便捷出口：先 embed 再 search（extractor 邻域检索用）。"""
        q_emb = self.embedder(text)
        if q_emb is None:
            return []
        return await self.search(project_id, q_emb, top)

    async def get_active(self, project_id: str) -> list[MemoryRecord]:
        """全量 active 记录（GC / 调试用）。"""
        rows = self.conn.execute(
            """SELECT * FROM memories WHERE project_id=? AND status='active'
               ORDER BY ts DESC""",
            (project_id,)).fetchall()
        return [self._row_to_rec(r) for r in rows]

    async def get_by_id(self, id: str) -> MemoryRecord | None:
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id=?", (id,)).fetchone()
        return self._row_to_rec(row) if row else None

    # ---------- GC ----------

    async def clusters(self, sim_threshold: float = 0.92,
                       project_id: str | None = None
                       ) -> list[list[MemoryRecord]]:
        """相似簇（GC 用）：active 记录两两余弦 → 连通簇。

        O(n²) 量级 <10k 可接受；GC 别在服务高峰跑（放离线任务 M19）。
        """
        if project_id:
            rows = self.conn.execute(
                """SELECT * FROM memories
                   WHERE project_id=? AND status='active'""",
                (project_id,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE status='active'").fetchall()
        records = [self._row_to_rec(r) for r in rows if json.loads(r["emb"])]
        n = len(records)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            parent[find(x)] = find(y)

        for i in range(n):
            for j in range(i + 1, n):
                if cosine(records[i].emb or [], records[j].emb or []) >= sim_threshold:
                    union(i, j)
        groups: dict[int, list[MemoryRecord]] = {}
        for i, r in enumerate(records):
            groups.setdefault(find(i), []).append(r)
        return [g for g in groups.values() if len(g) > 1]

    async def gc_merge_duplicates(self, sim_threshold: float = 0.92,
                                  project_id: str | None = None) -> int:
        """合并相似簇：保留新且重要者，其余归档。返回归档数。"""
        archived = 0
        for cluster in await self.clusters(sim_threshold, project_id):
            keep = max(cluster, key=lambda m: (m.ts, m.importance))
            for m in cluster:
                if m.id != keep.id:
                    await self.archive(m.id, reason=f"merged into {keep.id}")
                    archived += 1
        return archived

    # ---------- Profile 存储 ----------

    async def get_profile_data(self, project_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT data FROM profiles WHERE project_id=?",
            (project_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    async def save_profile_data(self, project_id: str, data: dict) -> None:
        self.conn.execute(
            """INSERT INTO profiles (project_id, data, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET data=?, updated_at=?""",
            (project_id, json.dumps(data, ensure_ascii=False), time.time(),
             json.dumps(data, ensure_ascii=False), time.time()))
        self.conn.commit()

    async def get_profile(self, project_id: str):
        """返回 ProjectProfile（延迟导入防循环依赖）。"""
        from .profile import ProjectProfile
        data = await self.get_profile_data(project_id)
        return ProjectProfile.from_dict(data) if data else ProjectProfile()

    # ---------- 内部 ----------

    @staticmethod
    def _row_to_rec(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"], kind=row["kind"], content=row["content"],
            emb=json.loads(row["emb"]), project_id=row["project_id"],
            session_id=row["session_id"], ts=row["ts"],
            importance=row["importance"], decay_days=row["decay_days"],
            source=row["source"], status=row["status"])


__all__ = ["Embedder", "MemoryRecord", "MemoryStore", "cosine", "fake_embed"]
