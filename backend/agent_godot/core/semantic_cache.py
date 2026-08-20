"""core/semantic_cache.py —— 认人的老门卫（M02 §1.5 / §4 步骤 8）

以 embedding 相似度为键的缓存：换个说法也认得。
三条红线：时间敏感 / 带指代 / 有工具副作用的请求绝不缓存
（红线③由调用方判定——req.tools 存在时适配器根本不查缓存）。
本阶段用假嵌入（哈希种子向量）跑通接口，M10 接真 bge 时只换 embedder。
"""

from __future__ import annotations

import hashlib
import math
import re
import time

_TIME_SENSITIVE = re.compile(r"(今天|昨天|最新|现在|目前|刚刚)")
_PRONOUNS = re.compile(r"(那|它|他|她|这个|刚才|继续|再来)")


def _fake_embed(text: str, dim: int = 64) -> list[float]:
    """假嵌入：文本哈希→种子→伪随机单位向量。确定性、零依赖。
    语义上当然不准——本阶段只为跑通缓存接口与阈值逻辑。"""
    state = int.from_bytes(hashlib.md5(text.encode()).digest()[:8], "big")
    vec = []
    for _ in range(dim):
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        vec.append(((state >> 33) / (1 << 31)) - 1.0)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # 已归一化：点积即余弦


class SemanticCache:
    def __init__(self, embedder=None, threshold: float = 0.92,
                 max_entries: int = 512, ttl: float = 86400.0):
        self._embed = embedder or _fake_embed
        self.threshold = threshold
        self.max_entries = max_entries
        self.ttl = ttl
        self._store: dict[str, tuple[list[float], str, float]] = {}

    def _last_user_text(self, messages) -> str | None:
        for m in reversed(messages):
            if getattr(m, "role", None) == "user" and m.content:
                return m.content
        return None

    def _blocked(self, text: str) -> bool:
        """红线①②：时间敏感、带指代。"""
        return bool(_TIME_SENSITIVE.search(text) or _PRONOUNS.search(text))

    def get(self, messages) -> str | None:
        """命中返回缓存答案，否则 None。"""
        q = self._last_user_text(messages)
        if q is None or self._blocked(q):
            return None
        qv = self._embed(q)
        now = time.time()
        best, best_sim = None, -1.0
        for _, (vec, answer, ts) in self._store.items():
            if now - ts > self.ttl:      # 过期惰性淘汰
                continue
            sim = _cos(qv, vec)
            if sim > best_sim:
                best, best_sim = answer, sim
        return best if best_sim >= self.threshold else None

    def put(self, messages, answer: str) -> None:
        q = self._last_user_text(messages)
        if q is None or self._blocked(q) or not answer:
            return
        if len(self._store) >= self.max_entries:   # 简单淘汰：丢最老
            oldest = min(self._store, key=lambda k: self._store[k][2])
            self._store.pop(oldest)
        self._store[q] = (self._embed(q), answer, time.time())
