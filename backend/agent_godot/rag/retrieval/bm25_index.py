r"""rag/retrieval/bm25_index.py —— 工程版稀疏索引（M10 §1.3 / §4 步骤 6）

与 lab/m10/bm25.py 的 MiniBM25 同公式（k₁=1.5 词频饱和 / b=0.75 长度归一 /
IDF+1 平滑），工程化差异在分词：
- 拉丁词整词保留：max_contacts_reported 这类 API 名不碎（BBPE 会拆散，
  嵌入空间反而糊——这正是 BM25 路存在的理由，§1.3 ②）
- 中文 jieba 分词（整句 \w+ 会把整句当一个词——lab 教学版的已知局限）

内存版暴力扫（M08 哲学：<10k 量级够用）；remove 走 O(N) 统计量重算——
删除低频发生，不值得为它维护增量 df 的复杂度。
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter

from ..chunking import Chunk

logger = logging.getLogger(__name__)

try:
    import jieba
    _jieba_cut = jieba.lcut
except ImportError:                      # 退化模式：逐字（精度降但可用）
    _jieba_cut = lambda s: list(s)       # noqa: E731

# 连续拉丁/数字/下划线 = 一个 token（API 名整词保留）
_LATIN_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def tokenize(text: str) -> list[str]:
    """拉丁整词 + 中文 jieba 分词（小写归一）。"""
    out: list[str] = []
    pos = 0
    for m in _LATIN_TOKEN.finditer(text):
        seg = text[pos:m.start()]
        if seg:
            out.extend(_cut_zh(seg))
        out.append(m.group(0).lower())
        pos = m.end()
    if pos < len(text):
        out.extend(_cut_zh(text[pos:]))
    return out


def _cut_zh(seg: str) -> list[str]:
    """中文段 jieba 分词；过滤纯标点/空白（至少含一个字母数字的 token 才留）。"""
    return [w for w in _jieba_cut(seg.lower())
            if w.strip() and any(ch.isalnum() for ch in w)]


class BM25Index:
    """内存 BM25：build / build_incremental / remove / search。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self._tf: dict[str, Counter] = {}       # chunk_id → 词频表
        self._len: dict[str, int] = {}           # chunk_id → token 数
        self._chunks: dict[str, Chunk] = {}      # chunk_id → Chunk（检索后回填用）
        self._df: Counter = Counter()
        self._avgdl = 0.0

    # ---------------- 维护 ----------------

    def build(self, chunks: list[Chunk]) -> None:
        """全量重建（清空旧库）。"""
        self._tf.clear()
        self._len.clear()
        self._chunks.clear()
        self.build_incremental(chunks)

    def build_incremental(self, chunks: list[Chunk]) -> None:
        """增量插入（同 chunk_id 覆盖）。"""
        for c in chunks:
            toks = tokenize(c.text)
            self._tf[c.chunk_id] = Counter(toks)
            self._len[c.chunk_id] = len(toks)
            self._chunks[c.chunk_id] = c
        self._rebuild_stats()

    def remove(self, chunk_ids: list[str]) -> int:
        """同步删（§3 一致性顺序的稀疏索引执行点）+ 统计量重算。"""
        n = 0
        for cid in chunk_ids:
            if self._tf.pop(cid, None) is not None:
                self._len.pop(cid, None)
                self._chunks.pop(cid, None)
                n += 1
        self._rebuild_stats()
        return n

    def _rebuild_stats(self) -> None:
        """df / avgdl 重算（O(总 token 数)——删除低频，教学取舍）。"""
        self._df = Counter(w for tf in self._tf.values() for w in tf)
        total = sum(self._len.values())
        self._avgdl = total / len(self._len) if self._len else 0.0

    # ---------------- 查询 ----------------

    @property
    def N(self) -> int:
        return len(self._tf)

    def _idf(self, w: str) -> float:
        # ★ +1 平滑：查询词不在库中（df=0）不除零（§1.3 易错点）
        return math.log((self.N - self._df[w] + 0.5) / (self._df[w] + 0.5) + 1)

    def search(self, query: str, top: int = 10) -> list[tuple[str, float]]:
        """BM25 打分 → [(chunk_id, score)] 降序（0 分 = 无命中词，不返回）。

        ★ 分数无界不可跨查询比较——只用于同查询内排序（§1.3 易错点）。
        """
        qs = tokenize(query)
        if not qs or not self._tf:
            return []
        scored: list[tuple[float, str]] = []
        for cid, tf in self._tf.items():
            dl = self._len[cid]
            norm = self.k1 * (1 - self.b + self.b * dl / self._avgdl) \
                if self._avgdl > 0 else self.k1
            s = 0.0
            for w in qs:
                f = tf.get(w, 0)
                if f:
                    s += self._idf(w) * (f * (self.k1 + 1)) / (f + norm)
            if s > 0:                            # ★ 0 分 = 查询词未命中，不进榜
                scored.append((s, cid))
        scored.sort(key=lambda x: (-x[0], x[1]))    # 分同分按 id 稳定排序
        return [(cid, s) for s, cid in scored[:top]]

    # ---------------- 便捷 ----------------

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        return self._chunks.get(chunk_id)

    def count(self, doc_id: str | None = None) -> int:
        if doc_id is None:
            return len(self._tf)
        return sum(1 for c in self._chunks.values() if c.doc_id == doc_id)


__all__ = ["BM25Index", "tokenize"]
