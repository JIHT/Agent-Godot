"""memory/retriever.py —— 三因子加权召回 + 渲染（M08 §1.3 / §4 步骤 2）

翻笔记的三条线索：相似度（主题相关）× 新鲜度（时间衰减）× 重要性（标注优先）。
默认权重 0.6/0.2/0.2——相似度主导（不相关的再新再重要也不该注入）。

注入格式用 <project_memory> 标签包裹 + 时间戳随行渲染（读取时标注），
system 声明"以工具实时读取为准"——防记忆幻觉的关键提示。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

from ..context.token_counter import TokenCounter
from .store import Embedder, MemoryRecord, MemoryStore, cosine, fake_embed


@dataclass
class RecallConfig:
    """召回配置：top-k / 三因子权重 / 注入预算。"""
    k: int = 8
    w_sim: float = 0.6
    w_rec: float = 0.2
    w_imp: float = 0.2
    max_tokens: int = 2000


@dataclass
class ScoredMemory:
    """一条召回结果：记录 + 总分 + 相似度（调试/审计用）。"""
    record: MemoryRecord
    score: float
    sim: float


class MemoryRetriever:
    """三因子加权召回 + XML 标签渲染。"""

    def __init__(self, store: MemoryStore,
                 embedder: Embedder | None = None,
                 config: RecallConfig | None = None,
                 counter: TokenCounter | None = None):
        self.store = store
        self.embedder: Embedder = embedder or store.embedder
        self.config = config or RecallConfig()
        self.counter = counter or TokenCounter()

    async def recall(self, project_id: str, query: str) -> list[ScoredMemory]:
        """召回：embed → 粗筛 top32 → 三因子加权精排 → 预算截断。"""
        q_emb = self.embedder(query)
        if q_emb is None:
            return []
        candidates = await self.store.search(project_id, q_emb, top=32)
        if not candidates:
            return []
        now = time.time()
        scored: list[ScoredMemory] = []
        for rec in candidates:
            sim = cosine(q_emb, rec.emb or [])
            days = max(0.0, (now - rec.ts) / 86400.0)
            recency = math.exp(-days / rec.decay_days) if rec.decay_days > 0 else 1.0
            score = (self.config.w_sim * sim
                     + self.config.w_rec * recency
                     + self.config.w_imp * rec.importance)
            scored.append(ScoredMemory(record=rec, score=score, sim=sim))
        scored.sort(key=lambda s: s.score, reverse=True)
        # token 预算截断
        cfg = self.config
        result: list[ScoredMemory] = []
        total = 0
        for s in scored[:cfg.k]:
            tokens = self.counter.estimate_text(s.record.content)
            if total + tokens > cfg.max_tokens and result:
                break
            result.append(s)
            total += tokens
        return result

    def render(self, scored: list[ScoredMemory]) -> str:
        """渲染为 <project_memory> 标签包裹 + 时间戳随行。

        声明"以工具实时读取为准"——记忆是先验不是事实，防记忆幻觉。
        """
        if not scored:
            return ""
        lines = [
            "<project_memory>",
            "以下是历史记忆，可能与当前代码状态不符，"
            "以工具实时读取为准（冲突时以新为准）：",
        ]
        for s in scored:
            ts_str = time.strftime("%Y-%m-%d", time.localtime(s.record.ts))
            src_tag = "" if s.record.source == "user_stated" else " [推断]"
            lines.append(f"[{ts_str}][{s.record.kind}]{src_tag} {s.record.content}")
        lines.append("</project_memory>")
        return "\n".join(lines)


def make_memory_provider(retriever: MemoryRetriever, project_id: str,
                         query_fn: Callable | None = None):
    """把 MemoryRetriever 包装成 ContextBuilder 的 memory_provider 回调。

    query_fn(session) → query 字符串；默认取最后一条 user 消息文本。
    用法：context = ContextBuilder(..., memory_provider=make_memory_provider(retriever, pid))
    """
    from ..core import Message

    async def provider(session) -> list:
        query = query_fn(session) if query_fn else _last_user_text(session)
        if not query:
            return []
        try:
            scored = await retriever.recall(project_id, query)
        except Exception:                           # noqa: BLE001 —— 记忆不可拖垮主流程
            return []
        rendered = retriever.render(scored)
        if rendered:
            return [Message(role="system", content=rendered)]
        return []
    return provider


def _last_user_text(session) -> str:
    for m in reversed(getattr(session, "messages", [])):
        if getattr(m, "role", None) == "user" and m.content:
            return m.content
    return ""


__all__ = ["MemoryRetriever", "RecallConfig", "ScoredMemory",
           "make_memory_provider"]
