"""联网原语：信封防注入 · 降级纪律 · 域名可信度（M12 §1.5 / §5）。"""
from __future__ import annotations

import httpx

from agent_godot.query_engine import (SearchEngine, SearchHit,
                                      WebSearchProvider, domain_trust)
from agent_godot.query_engine.web_provider import _extract_main

from .conftest import AREA2D_HTML, HITS, MockSearchEngine, mock_client


async def test_fetch_wraps_content_in_envelope(web_provider):
    """验收 §5：本地 HTML → 抽出正文且包裹 <untrusted_data> 信封。"""
    page = await web_provider.fetch(
        "https://docs.godotengine.org/stable/area2d.html")
    assert page.startswith('<untrusted_data source=')
    assert page.rstrip().endswith("</untrusted_data>")
    assert "body_entered" in page
    # 抽的是正文：导航/页脚/script 不该进信封（trafilatura 或兜底抽取）
    assert "console.log" not in page


async def test_gather_degrades_failed_pages_to_snippet(web_provider):
    """前 3 个 URL 中 1 个连不上 → 该条 fetched=False 仅留 snippet，不抛错。"""
    results = await web_provider.gather("Godot Area2D body_entered", n=5)
    assert len(results) == 3
    failed = [r for r in results if not r.fetched]
    assert failed and all(r.snippet for r in failed)
    assert failed[0].content is None
    # 成功页有信封化正文
    ok = [r for r in results if r.fetched]
    assert ok and all(r.content and r.content.startswith(
        "<untrusted_data") for r in ok)


async def test_short_extract_degrades_to_snippet(search_engine):
    """空壳页（抽取 <200 chars，SPA）→ 降级只留 snippet。"""
    provider = WebSearchProvider(
        search_engine,
        client=mock_client({
            "https://docs.godotengine.org/stable/area2d.html": "<html></html>",
            "https://godotengine.org/release-notes": "<html></html>",
            "https://forum.example.com/t/area2d": "<html></html>",
        }))
    results = await provider.gather("query", n=3)
    assert all(not r.fetched and r.content is None for r in results)


async def test_gather_uses_only_top_k_pages(search_engine):
    """预算纪律：max_pages=3 封顶，第 4 条线索只出现在 search() 不进 gather。"""
    engine = MockSearchEngine(HITS)
    provider = WebSearchProvider(
        engine, max_pages=2,
        client=mock_client({
            "https://docs.godotengine.org/stable/area2d.html": AREA2D_HTML,
            "https://godotengine.org/release-notes": AREA2D_HTML,
        }))
    results = await provider.gather("query", n=5)
    assert len(results) == 2


async def test_domain_trust_weights_official_docs():
    """官方文档 > 教程站 > 未知站（质量参差的第一道防线）。"""
    assert domain_trust("https://docs.godotengine.org/x") == 1.0
    assert domain_trust("https://www.docs.godotengine.org/x") == 1.0
    assert domain_trust("https://stackoverflow.com/q/1") == 0.6
    assert domain_trust("https://unknown.example.com/x") == 0.5


async def test_search_only_returns_snippets(search_engine):
    """轻量入口：search 只拿线索不抓正文（content=None）。"""
    provider = WebSearchProvider(
        search_engine, client=mock_client({}))
    results = await provider.search("Godot Area2D", n=2)
    assert len(results) == 2
    assert all(r.content is None and not r.fetched for r in results)


def test_extract_main_strips_scripts():
    """正文抽取红线：script/style 不进正文（防 tracker/注入载体）。"""
    html = "<html><body><p>body_entered 触发检测</p>" \
           "<script>evil()</script></body></html>"
    text = _extract_main(html)
    assert "body_entered" in text
    assert "evil" not in text


async def test_engine_protocol_shape():
    """SearchEngine 只认形状：任何带 search 的对象都能上岗。"""

    class Duck:
        async def search(self, q, n):
            return [SearchHit("t", "https://x.com", "s")]

    provider = WebSearchProvider(Duck(), client=mock_client({}))
    hits = await provider.search("q", n=1)
    assert hits[0].title == "t"
