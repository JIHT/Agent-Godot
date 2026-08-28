"""M12 Query Engine 测试公共 fixtures。

教学版惯例：测试不依赖 lab 目录、不依赖外网/真实模型——
- FakeLLM：剧本式 complete()（意图/改写消费 M02 协议，只认形状）；
- MockSearchEngine + httpx.MockTransport：联网全链路的本地替身。
"""
from __future__ import annotations

import httpx
import pytest

from agent_godot.core import LLMResponse
from agent_godot.query_engine import (IntentClassifier, QueryRewriter,
                                      SearchHit, WebSearchProvider)

# 一篇够长的 Godot 文档页（≥200 chars 才不会被当 SPA 空壳页降级）
AREA2D_HTML = """<html><head><title>Using Area2D</title>
<style>.nav{color:red}</style></head><body>
<nav>Home Docs Blog Forum</nav>
<article><h1>Using Area2D</h1>
<p>Area2D 是 Godot 中用于检测区域重叠的节点。body_entered 信号在
monitoring 与 monitorable 同时为 true 时触发，常用于拾取物与伤害判定。
area_entered 则用于 Area2D 之间的相互检测。信号处理函数通常挂接到
场景树的节点脚本上，通过 connection 面板或代码 connect 完成。</p>
<p>注意：Area2D 继承自 CollisionObject2D，与 StaticBody2D 的区别在于
前者不参与物理碰撞响应，只做检测。检测层与掩码的配置决定了哪些对象
会触发信号。</p></article>
<footer>© Godot Engine</footer>
<script>console.log("tracker");</script></body></html>"""


class FakeLLM:
    """剧本式假模型：complete 按序吐预置文本（越界循环用最后一段）。"""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0
        self.requests: list = []

    async def complete(self, req):
        self.requests.append(req)
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return LLMResponse(content=self.responses[idx], tool_calls=[],
                           usage=None, finish_reason="stop")


class MockSearchEngine:
    """固定结果的假搜索引擎（gather/search 共用）。"""

    def __init__(self, hits: list[SearchHit]):
        self.hits = hits
        self.queries: list[str] = []

    async def search(self, query: str, n: int) -> list[SearchHit]:
        self.queries.append(query)
        return self.hits[:n]


def mock_client(pages: dict[str, str],
                fail_urls: set[str] | None = None) -> httpx.AsyncClient:
    """本地 httpx 替身：命中的 URL 回 pages 文本，fail_urls 抛连接错误。"""
    fail = fail_urls or set()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in fail:
            raise httpx.ConnectError(f"refused: {url}")
        if url in pages:
            return httpx.Response(200, text=pages[url])
        return httpx.Response(404, text="not found")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


HITS = [
    SearchHit(title="Using Area2D - Godot Docs",
              url="https://docs.godotengine.org/stable/area2d.html",
              snippet="body_entered 信号在 monitoring 为 true 时触发"),
    SearchHit(title="Godot 4.4 release notes",
              url="https://godotengine.org/release-notes",
              snippet="Godot 4.4 发布说明"),
    SearchHit(title="论坛搬运 - Area2D",
              url="https://forum.example.com/t/area2d",
              snippet="Area2D discussion"),
]


@pytest.fixture
def intent_llm() -> FakeLLM:
    return FakeLLM(["knowledge"])


@pytest.fixture
def rewriter_llm() -> FakeLLM:
    return FakeLLM(["Area2D 的检测信号 body_entered"])


@pytest.fixture
def classifier(intent_llm) -> IntentClassifier:
    return IntentClassifier(intent_llm, model="fake-model")


@pytest.fixture
def rewriter(rewriter_llm) -> QueryRewriter:
    return QueryRewriter(rewriter_llm, model="fake-model")


@pytest.fixture
def search_engine() -> MockSearchEngine:
    return MockSearchEngine(HITS)


@pytest.fixture
def web_provider(search_engine) -> WebSearchProvider:
    provider = WebSearchProvider(
        search_engine,
        client=mock_client({
            "https://docs.godotengine.org/stable/area2d.html": AREA2D_HTML,
            "https://godotengine.org/release-notes": AREA2D_HTML,
            "https://forum.example.com/t/area2d": "<html><body></body></html>",
        }, fail_urls={"https://forum.example.com/t/area2d"}))
    return provider
