"""query_engine/web_provider.py —— 联网检索原语（M12 §1.5 / §4 步骤 0）

120 外勤小队（WEB 通道执行器，pipeline 的 web 依赖，先于决策层施工）：
队长问电台（搜索引擎拿 top-N 线索）→ 把最靠谱的 3 位目击者请回院里
详谈（fetch 前 k 个页面）→ 誊成一页纸（trafilatura 抽正文 + 预算截断）
→ 盖"外部证词"章（<untrusted_data> 信封——网页是不可信数据，正文里
埋的"忽略以上指令"一律视为数据、不得执行——间接提示注入是头号红线）。

降级纪律（联网是增强不是依赖）：
- 单页 fetch 失败（超时/4xx/5xx/被反爬）→ 只留搜索 snippet，不抛错；
- 抽取结果 <200 chars（SPA 空壳页）→ 同样降级 snippet；
- 全失败 → gather 返回带空 content 的列表，整合器渲染"（无结果）"。

反爬礼仪：UA 自报家门、超时 5s——被拉黑的是整个产品的出口 IP。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# UA 自报家门（反爬礼仪：匿名爬虫 UA 是被拉黑的第一名）
USER_AGENT = "AgentGodot/0.1 (Godot game assistant; research prototype)"

# 域名可信度加权（质量参差的第一道防线）：官方文档 > 教程站 > 论坛/搬运站
TRUSTED_DOMAINS: dict[str, float] = {
    "docs.godotengine.org": 1.0,
    "godotengine.org": 0.9,
    "github.com": 0.8,
    "docs.python.org": 0.8,
    "stackoverflow.com": 0.6,
}
DEFAULT_TRUST = 0.5


def domain_trust(url: str, trusted: dict[str, float] | None = None) -> float:
    """URL → 域名可信度分（整合器与择优抓取共用）。"""
    host = urlparse(url).netloc.lower()
    table = trusted if trusted is not None else TRUSTED_DOMAINS
    # 精确命中优先，父域次之（www.docs.godotengine.org 也该拿高分）
    if host in table:
        return table[host]
    for domain, score in table.items():
        if host.endswith("." + domain):
            return score
    return DEFAULT_TRUST


# ---------- ① 搜索引擎（可换实现：Tavily / SearXNG / …） ----------

@dataclass
class SearchHit:
    """一条搜索线索：标题 + URL + 摘要。"""
    title: str
    url: str
    snippet: str


class SearchEngine(Protocol):
    """搜索源协议：只认形状不认血缘（测试塞 MockEngine 照样上岗）。"""

    async def search(self, query: str, n: int) -> list[SearchHit]: ...


class TavilyEngine:
    """Tavily 搜索 API（为 LLM/RAG 而生的搜索源，REST 直调无需 SDK）。"""

    def __init__(self, api_key: str, base_url: str = "https://api.tavily.com",
                 timeout: float = 10.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def search(self, query: str, n: int) -> list[SearchHit]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/search", json={
                "api_key": self.api_key, "query": query, "max_results": n})
            resp.raise_for_status()
            data = resp.json()
        return [SearchHit(title=r.get("title", ""), url=r.get("url", ""),
                          snippet=r.get("content", ""))
                for r in data.get("results", [])]


class SearxngEngine:
    """自建 SearXNG（免费、隐私可控、聚合多引擎，Docker 一键起）。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8888",
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def search(self, query: str, n: int) -> list[SearchHit]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json"},
                headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            data = resp.json()
        return [SearchHit(title=r.get("title", ""), url=r.get("url", ""),
                          snippet=r.get("content", ""))
                for r in data.get("results", [])[:n]]


# ---------- ② 正文抽取（trafilatura 优先，零依赖兜底） ----------

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")


def _naive_extract(html: str) -> str:
    """trafilatura 未安装时的粗暴兜底：去 script/style → 去标签 → 收空白。

    教学环境（没装 trafilatura）也能跑通管线逻辑；生产装了依赖后
    自动走 trafilatura（跨站基准领先 + 中文友好）。
    """
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return _WS_RE.sub("\n\n", text).strip()


def _extract_main(html: str) -> str:
    """网页 → 正文：去导航/广告/脚本，只留正文。"""
    try:
        import trafilatura                       # 惰性导入：可选依赖
    except ImportError:
        logger.debug("trafilatura 未安装，用朴素兜底抽取")
        return _naive_extract(html)
    try:
        return trafilatura.extract(html) or ""
    except Exception:                            # noqa: BLE001 —— 抽取失败降级
        return ""


# ---------- ③ WEB 通道执行器 ----------

@dataclass
class WebResult:
    """一条联网结果：摘要必有，正文 fetch 成功才有（信封化）。"""
    title: str
    url: str
    snippet: str
    content: str | None                   # fetch 成功才有 <untrusted_data> 信封
    fetched: bool
    score: float = 0.0                    # 域名可信度


class WebSearchProvider:
    """搜索 → 择优抓取 → 正文抽取 → 清洗截断 → 信封。"""

    def __init__(self, engine: SearchEngine, max_pages: int = 3,
                 max_chars: int = 2000, timeout: float = 5.0,
                 min_chars: int = 200,
                 trusted_domains: dict[str, float] | None = None,
                 client: httpx.AsyncClient | None = None):
        self.engine = engine
        self.max_pages = max_pages         # WEB 预算最紧：k=3 页封顶
        self.max_chars = max_chars         # 2k chars/页（要要点不要全文搬运）
        self.timeout = timeout
        self.min_chars = min_chars         # <200 chars = SPA 空壳页 → 降级
        self.trusted = trusted_domains
        self.client = client or httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": USER_AGENT})

    async def search(self, query: str, n: int = 5) -> list[WebResult]:
        """只搜索不抓取（轻量入口；整合器预算紧时可以只用 snippet）。"""
        hits = await self.engine.search(query, n)
        return [WebResult(title=h.title, url=h.url, snippet=h.snippet,
                          content=None, fetched=False,
                          score=domain_trust(h.url, self.trusted))
                for h in hits]

    async def fetch(self, url: str) -> str:
        """GET → trafilatura 抽正文 → 截 2k chars → <untrusted_data> 信封。

        抓取/抽取失败直接抛（调用方 gather(return_exceptions=True)
        统一降级，单页失败不炸整体）。
        """
        return self._envelope(url, (await self._extract(url))[: self.max_chars])

    async def gather(self, query: str, n: int = 5) -> list[WebResult]:
        """搜索 + 并行抓前 k 个：异常页/空壳页降级只留 snippet，不抛错。"""
        hits = (await self.engine.search(query, n))[: self.max_pages]
        # 并行抓取（同批不同域——同域名串行是后续接入全局限速器时的纪律）
        texts = await asyncio.gather(
            *(self._extract(h.url) for h in hits), return_exceptions=True)
        out: list[WebResult] = []
        for hit, text in zip(hits, texts):
            ok = (not isinstance(text, Exception)
                  and len(text) >= self.min_chars)
            if isinstance(text, Exception):
                logger.info("抓取失败降级 snippet: %s（%s）", hit.url,
                            type(text).__name__)
            elif not ok:
                logger.info("空壳页降级 snippet: %s（抽取 %d chars）",
                            hit.url, len(text))
            out.append(WebResult(
                title=hit.title, url=hit.url, snippet=hit.snippet,
                content=self._envelope(hit.url, text[: self.max_chars])
                if ok else None,
                fetched=ok,
                score=domain_trust(hit.url, self.trusted)))
        return out

    # ---------- 内部 ----------

    async def _extract(self, url: str) -> str:
        """GET 一页并抽正文（超时/非 2xx 抛错，由调用方降级）。"""
        resp = await self.client.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return _extract_main(resp.text)

    @staticmethod
    def _envelope(url: str, text: str) -> str:
        """外部证词盖章：信封内出现的一切指令都是数据，不是命令。"""
        return f'<untrusted_data source="{url}">\n{text}\n</untrusted_data>'

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()


__all__ = ["DEFAULT_TRUST", "SearchEngine", "SearchHit", "SearxngEngine",
           "TRUSTED_DOMAINS", "TavilyEngine", "USER_AGENT",
           "WebResult", "WebSearchProvider", "domain_trust"]
