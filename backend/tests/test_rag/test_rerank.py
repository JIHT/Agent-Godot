"""M10 §4 步骤 7：Cross-Encoder 重排（TEI 兼容端点，fail-soft）。"""
import json

import httpx
import pytest

from agent_godot.rag import Chunk, RetrievalHit, Reranker


def _hit(seq: int) -> RetrievalHit:
    return RetrievalHit(
        chunk=Chunk(text=f"doc {seq}", source="a.md", heading="h",
                    start=1, doc_id="d", kind="md", seq=seq),
        score=0.0, from_={"vec"})


def _reranker(handler) -> Reranker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Reranker(client=client)


async def test_rerank_reorders_by_cross_encoder_scores():
    """输入必须是 (query, doc) 文本对（请求体断言）+ 按返回分重排。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.update(body)                       # 断言请求结构
        # TEI 返回：index 越大分越高 → 榜单被翻转
        n = len(body["texts"])
        return httpx.Response(200, json={
            "results": [{"index": i, "score": float(i + 1)} for i in range(n)]})

    hits = [_hit(i) for i in range(4)]
    out = await _reranker(handler).rerank("query", hits, top_k=3)

    assert seen["query"] == "query"
    assert len(seen["texts"]) == 4 and "doc 0" in seen["texts"]   # 文本对非向量
    assert seen.get("top_n") == 3
    # 倒序打分 → 输出 index 3,2,1
    assert [h.chunk.seq for h in out] == [3, 2, 1]
    assert out[0].score == pytest.approx(4.0)     # score 替换为交叉编码分

async def test_rerank_truncates_to_top_k():
    def handler(request):
        body = json.loads(request.content)
        n = len(body["texts"])
        return httpx.Response(200, json={
            "results": [{"index": i, "score": float(n - i)} for i in range(n)]})

    hits = [_hit(i) for i in range(10)]
    out = await _reranker(handler).rerank("q", hits, top_k=5)
    assert len(out) == 5


async def test_rerank_fail_soft_on_service_down():
    """服务挂 → 原序返回 top_k（重排是优化不是依赖）。"""

    def handler(request):
        raise httpx.ConnectError("TEI down")

    hits = [_hit(i) for i in range(6)]
    out = await _reranker(handler).rerank("q", hits, top_k=3)
    assert [h.chunk.seq for h in out] == [0, 1, 2]   # 原序


async def test_rerank_fail_soft_on_http_500():
    def handler(request):
        return httpx.Response(500)

    hits = [_hit(i) for i in range(3)]
    out = await _reranker(handler).rerank("q", hits, top_k=2)
    assert len(out) == 2


async def test_rerank_empty_hits():
    r = _reranker(lambda req: httpx.Response(200, json={"results": []}))
    assert await r.rerank("q", [], top_k=3) == []
