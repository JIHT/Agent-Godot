"""rag/rerank.py —— Cross-Encoder 精排（M10 §1.5 / §4 步骤 7）

双塔粗排（毫秒级百万→50）管召回，交叉编码精排（百毫秒级 50→10）管排序：
query 与 doc 拼接后一起进模型，注意力层直接做词级对齐——
★ 输入是 (query, doc_text) 文本对，喂向量等于把交互机会提前杀死（§1.5 易错点）。

fail-soft：重排是优化不是依赖——TEI 服务挂了原序返回 top_k
（同 M02 语义缓存哲学：缓存让路、不拖垮主路径）。
"""
from __future__ import annotations

import logging
from dataclasses import replace

import httpx

from .retrieval import RetrievalHit

logger = logging.getLogger(__name__)


class Reranker:
    """TEI 兼容 /rerank 端点封装（bge-reranker-v2-m3）。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8080", model: str = "",
                 timeout: float = 30.0, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model          # TEI 单模型部署可不填
        self.timeout = timeout
        self._client = client      # 可注入（测试用 MockTransport）

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def rerank(self, query: str, hits: list[RetrievalHit],
                     top_k: int = 5) -> list[RetrievalHit]:
        """交叉编码重排：hits 按 (query, doc) 对相关性重排序，截断 top_k。

        服务失败 → 原序返回前 top_k（检索结果不因优化层缺席而变空）。
        注意：不用阈值砍"全空也不放过"——有时全体不相关，
        应让模型看到空结果说"知识库未覆盖"（§1.5 易错点）。
        """
        if not hits:
            return []
        try:
            payload: dict = {
                "query": query,
                "texts": [h.chunk.text for h in hits],   # 文本对，不是向量！
                "top_n": top_k,
                "return_text": False,
            }
            if self.model:
                payload["model"] = self.model
            resp = await self._get_client().post(f"{self.base_url}/rerank",
                                                 json=payload)
            resp.raise_for_status()
            results = resp.json()["results"]     # [{"index": i, "score": s}]
            out: list[RetrievalHit] = []
            for r in sorted(results, key=lambda r: -r["score"])[:top_k]:
                hit = hits[r["index"]]
                # score 替换为交叉编码分（from_ 保留——来源仍可审计）
                out.append(replace(hit, score=float(r["score"])))
            return out
        except Exception as e:                   # noqa: BLE001 —— fail-soft
            logger.warning("重排服务不可用，原序返回: %s", e)
            return hits[:top_k]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["Reranker"]
