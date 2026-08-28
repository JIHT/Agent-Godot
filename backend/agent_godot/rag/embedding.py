"""rag/embedding.py —— bge-m3 接入层（M10 §1.2 / §4 步骤 5）

一个事实源三用途（RAG 嵌入 / M02 语义缓存 / M08 记忆嵌入）——
同一模型保证相似度空间一致：各用各的尺子，0.92 的阈值换个空间就失效。

端点形态：OpenAI 兼容 /v1/embeddings（ollama / TEI / vLLM 通吃——
部署形态变，调用方接口不变）。换嵌入模型 = 维度变 = 全量重建库（铁律，
VectorIndex 侧的维度守卫见 M10 §1.2 易错点）。
"""
from __future__ import annotations

import hashlib
import logging
import math

import httpx

logger = logging.getLogger(__name__)

# ★ bge 系列 query 要加指令前缀，document 不加——漏掉召回率掉 5~10 个点（M01 埋坑在此兑现）
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


def _l2_normalize(vec: list[float]) -> list[float]:
    """归一化后 IP(内积) == 余弦（§1.2 易错点：未归一化时两者不等）。"""
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm > 0 else vec


class EmbeddingService:
    """批量/单条嵌入：embed_documents（建库，不加前缀）+ embed_query（检索，加前缀）。"""

    def __init__(self, base_url: str = "http://127.0.0.1:11434/v1",
                 model: str = "bge-m3", dim: int = 1024,
                 batch_size: int = 32, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim                  # bge-m3 = 1024，必须与 collection 一致
        self.batch_size = batch_size
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _serve(self, texts: list[str]) -> list[list[float]]:
        """批量调端点（分批防超时）+ L2 归一化。"""
        out: list[list[float]] = []
        client = self._get_client()
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            resp = await client.post(f"{self.base_url}/embeddings",
                                     json={"model": self.model, "input": batch})
            resp.raise_for_status()
            data = resp.json()["data"]
            out.extend(_l2_normalize(d["embedding"]) for d in data)
        return out

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """文档侧编码：不加前缀。"""
        return await self._serve(texts)

    async def embed_query(self, q: str) -> list[float]:
        """查询侧编码：加 bge 指令前缀。"""
        return (await self._serve([QUERY_PREFIX + q]))[0]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _fake_embed(text: str, dim: int = 64) -> list[float]:
    """假嵌入：文本哈希→种子→伪随机单位向量（与 memory.fake_embed 同源同算法）。

    确定性、零依赖：同句自查可命中，换说法永不命中——
    足够跑通检索管线逻辑，CI/单测专用。
    """
    state = int.from_bytes(hashlib.md5(text.encode()).digest()[:8], "big")
    vec: list[float] = []
    for _ in range(dim):
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        vec.append(((state >> 33) / (1 << 31)) - 1.0)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class FakeEmbeddingService:
    """假嵌入服务（测试/CI）：接口与 EmbeddingService 同形，插槽即插即用。

    注意：假向量无语义，embed_query 不模拟 bge 前缀（对 hash 无意义）。
    """

    def __init__(self, dim: int = 64):
        self.dim = dim

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [_fake_embed(t, self.dim) for t in texts]

    async def embed_query(self, q: str) -> list[float]:
        return _fake_embed(q, self.dim)

    async def close(self) -> None:
        pass


__all__ = ["EmbeddingService", "FakeEmbeddingService", "QUERY_PREFIX"]
