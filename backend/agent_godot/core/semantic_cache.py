"""core/semantic_cache.py —— 认人的老门卫（M02 §1.5 / §4 步骤 8）

以 embedding 相似度为键的缓存：换个说法也认得。
三条红线：时间敏感 / 带指代 / 有工具副作用的请求绝不缓存
（红线③由调用方判定——req.tools 存在时适配器根本不查缓存）。

嵌入来源两级（models.yaml 的 embedding 节装配，见 config.py）：
- 默认 _fake_embed：哈希伪随机向量——零依赖、确定性，CI/单测环境用；
  无语义能力（换说法永远不命中），只为跑通红线/TTL/阈值等缓存逻辑。
- OllamaEmbedder：本地 ollama + bge-m3 真嵌入（1024 维）。
  fail-soft：嵌入服务挂了返回 None → 缓存自动让路，主路径照常推理——
  缓存是优化，不是依赖，绝不因嵌入服务宕机拖垮网关。

维度守卫：换嵌入模型（64 维假 → 1024 维 bge-m3）时，旧条目与新查询
维度不符，_cos 的 zip 会静默按短截断（不报错但结果全错）——
get/put 入口校验维度漂移，漂移即全量失效（呼应 M10 铁律：
换嵌入模型 = 重建全部索引与缓存）。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time

import httpx

logger = logging.getLogger(__name__)

# 红线①：时间敏感——命中缓存 = 给过期答案（词表式兜底，M12 意图分类终审）
_TIME_SENSITIVE = re.compile(
    r"(今天|昨天|明天|最新|现在|目前|刚刚|最近|上周|上个月|去年|今年)")
# 红线②：带指代/追问——必须先过 M12 改写归一化，绝不能直接查缓存
_PRONOUNS = re.compile(r"(那|它|他|她|这个|刚才|继续|再来)")


def _fake_embed(text: str, dim: int = 64) -> list[float]:
    """假嵌入：文本哈希→种子→伪随机单位向量。确定性、零依赖。
    语义上当然不准——同一段文本永远得到同一个向量（原句自查可命中），
    不同文本之间则是随机方向（换说法永不命中）。CI/单测专用。"""
    state = int.from_bytes(hashlib.md5(text.encode()).digest()[:8], "big")
    vec = []
    for _ in range(dim):
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        vec.append(((state >> 33) / (1 << 31)) - 1.0)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class OllamaEmbedder:
    """真嵌入：调本地 ollama 的 OpenAI 兼容 /v1/embeddings 端点（bge-m3）。

    设计要点：
    - fail-soft：任何失败（服务未启动/超时/模型缺失）返回 None，
      SemanticCache 看到 None 自动跳过缓存——绝不拖垮推理主路径
    - 同步 httpx：单条编码 ~50-300ms（CPU），CLI 单用户场景可接受；
      M19 多租户前需改 async 或 to_thread（当前阶段刻意保持简单）
    - ollama / bge-m3 返回已 L2 归一化向量 → 点积即余弦
    - 可调用对象（__call__）：与 _fake_embed 同签名，插槽即插即用
    """

    def __init__(self, base_url: str = "http://127.0.0.1:11434/v1",
                 model: str = "bge-m3", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.Client(timeout=timeout)
        self.dim: int | None = None          # 首次成功编码后锁定（bge-m3=1024）

    def __call__(self, text: str) -> list[float] | None:
        try:
            resp = self._client.post(
                f"{self.base_url}/embeddings",
                json={"model": self.model, "input": [text]})
            resp.raise_for_status()
            vec: list[float] = resp.json()["data"][0]["embedding"]
            self.dim = len(vec)
            return vec
        except Exception as e:               # noqa: BLE001 —— 缓存必须 fail-soft
            logger.debug("嵌入服务不可用，语义缓存让路: %s", e)
            return None

    def close(self) -> None:
        self._client.close()


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
        self._locked_dim: int | None = None  # 维度守卫：随首个条目锁定

    def _last_user_text(self, messages) -> str | None:
        """取最后一条 user 消息文本。兼容 Message dataclass（网关主路径）
        与 dict（lab 实验/测试便利形态）两种消息形态。"""
        for m in reversed(messages):
            if isinstance(m, dict):
                role, content = m.get("role"), m.get("content")
            else:
                role = getattr(m, "role", None)
                content = getattr(m, "content", None)
            if role == "user" and content:
                return content
        return None

    def _blocked(self, text: str) -> bool:
        """红线①②：时间敏感、带指代。"""
        return bool(_TIME_SENSITIVE.search(text) or _PRONOUNS.search(text))

    def _guard_dim(self, vec: list[float]) -> None:
        """换嵌入模型守卫：维度漂移 → 全量失效。

        背景：_cos 的 zip 按短边静默截断——64 维旧条目对 1024 维新查询
        不报错但相似度全错。守卫保证"换模型 = 缓存清零"自动发生。
        """
        if self._locked_dim is None:
            self._locked_dim = len(vec)     # 首个条目锁定基准
        elif len(vec) != self._locked_dim:
            old = self._locked_dim
            self._store.clear()             # ★ 全量失效，从零重新积累
            self._locked_dim = len(vec)
            logger.info("嵌入维度 %s→%s（换嵌入模型），缓存全量失效", old, len(vec))

    def get(self, messages) -> str | None:
        """命中返回缓存答案，否则 None。"""
        q = self._last_user_text(messages)
        if q is None or self._blocked(q):
            return None
        qv = self._embed(q)
        if qv is None:                       # embedder 失效 → 让路（fail-soft）
            return None
        self._guard_dim(qv)
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
        qv = self._embed(q)
        if qv is None:                       # embedder 失效 → 放弃本条
            return
        self._guard_dim(qv)
        if len(self._store) >= self.max_entries:   # 简单淘汰：丢最老
            oldest = min(self._store, key=lambda k: self._store[k][2])
            self._store.pop(oldest)
        self._store[q] = (qv, answer, time.time())
