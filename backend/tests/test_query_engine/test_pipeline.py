"""全链编排：ambiguous 回路 · 零检索 · 双通道注入 · 通道降级（§3 / §5）。"""
from __future__ import annotations

import pytest

from agent_godot.core import Message
from agent_godot.graphrag import GraphPath
from agent_godot.query_engine import (Channel, Intent, IntentClassifier,
                                      QueryEngine, QueryResult, QueryRouter,
                                      QueryRewriter, RoutingContext,
                                      rag_messages)
from agent_godot.rag import Chunk, RetrievalHit

from .conftest import FakeLLM


def _hit(doc_id: str, text: str, source: str = "docs/area2d.md") -> RetrievalHit:
    chunk = Chunk(text=text, source=source, heading="signals",
                  start=1, doc_id=doc_id, kind="md", seq=0)
    return RetrievalHit(chunk=chunk, score=1.0, from_={"bm25"})


class StubRAG:
    """记录调用的假检索器：永远命中一条 Area2D chunk。"""

    def __init__(self, hits: list[RetrievalHit] | None = None):
        self.queries: list[str] = []
        self.hits = hits if hits is not None else [
            _hit("doc1", "body_entered 在 monitoring=true 时触发。")]

    async def retrieve(self, q: str, top_k: int = 3) -> list[RetrievalHit]:
        self.queries.append(q)
        return self.hits[:top_k]


class StubGraph:
    """记录调用的假图路：永远给一条推理链。"""

    def __init__(self, paths: list[GraphPath] | None = None):
        self.queries: list[str] = []
        self.paths = paths or [GraphPath(
            nodes=["body_entered", "Main", "main.gd"],
            edges=["LISTENS", "ATTACHED_SCRIPT"])]

    async def trace(self, q: str, project_id: str) -> list[GraphPath]:
        self.queries.append(q)
        return self.paths


class BoomWeb:
    """必炸的联网通道：验证 fail-soft。"""

    async def gather(self, q: str, n: int = 5):
        raise RuntimeError("network down")


def make_engine(responses: list[str], rag=None, graph=None, web=None,
                hyde: bool = False) -> QueryEngine:
    """responses 剧本同时喂给意图与改写（两次 classify 一次 rewrite 场景
    用 'ambiguous'/'改写结果'/'knowledge' 的顺序）。"""
    llm = FakeLLM(responses)
    return QueryEngine(
        IntentClassifier(llm, model="m"),
        QueryRewriter(FakeLLM(responses), model="m"),
        QueryRouter(), rag or StubRAG(), graph or StubGraph(), web,
        hyde_enabled=hyde)


CTX = RoutingContext(kb_enabled=True, graph_ready=False, project_id="p1")


async def test_followup_rewritten_before_retrieval():
    """验收 §5①②：上文 Area2D，追问"它的信号" → 检索参数含 Area2D。"""
    history = [Message(role="user", content="Area2D 怎么检测碰撞？"),
               Message(role="assistant", content="用 body_entered。")]
    rag = StubRAG()
    engine = QueryEngine(
        IntentClassifier(FakeLLM(["ambiguous", "knowledge"]), model="m"),
        QueryRewriter(FakeLLM(["Area2D 的检测信号 body_entered"]), model="m"),
        QueryRouter(), rag, StubGraph(), None)
    result = await engine.process("那它的信号呢", history, CTX)
    assert result.intent is Intent.KNOWLEDGE
    assert "Area2D" in rag.queries[0]      # 改写发生在检索之前
    assert result.trace["intent_2nd"] == "knowledge"


async def test_ambiguous_second_pass_still_fuzzy_goes_conservative():
    """二次分类仍模糊 → 保守默认 knowledge（§3 回路封顶）。"""
    engine = QueryEngine(
        IntentClassifier(FakeLLM(["ambiguous", "ambiguous"]), model="m"),
        QueryRewriter(FakeLLM(["还是模糊的句子"]), model="m"),
        QueryRouter(), StubRAG(), StubGraph(), None)
    result = await engine.process("那这个呢", [
        Message(role="user", content="随便聊聊")], CTX)
    assert result.intent is Intent.KNOWLEDGE
    assert result.trace["intent_fallback"]


async def test_chitchat_zero_retrieval_calls():
    """验收 §5③：闲聊零检索（省钱省延迟），不注入任何块。"""
    rag, graph = StubRAG(), StubGraph()
    engine = make_engine(["chitchat"], rag=rag, graph=graph)
    result = await engine.process("谢谢！", [], CTX)
    assert result.plan.channels == []
    assert rag.queries == [] and graph.queries == []
    assert result.context_block == ""


async def test_multi_hop_dual_channel_injection():
    """验收 §5④：多跳问题 → GRAPH+RAG 双通道注入（推理链+文档引用）。"""
    engine = make_engine(["knowledge"], rag=StubRAG(), graph=StubGraph())
    ctx = RoutingContext(kb_enabled=True, graph_ready=True, project_id="p1")
    result = await engine.process("删掉 hitbox 信号影响哪些场景", [], ctx)
    assert result.plan.channels == [Channel.GRAPH, Channel.RAG]
    assert "[项目图谱]" in result.context_block
    assert "--LISTENS-->" in result.context_block
    assert "[知识库]" in result.context_block
    assert "body_entered" in result.context_block


async def test_web_channel_injection_with_envelope(web_provider):
    """联网通道注入带 URL 引用与信封（§5⑤ search 意图全链）。"""
    engine = make_engine(["search"], rag=StubRAG(), graph=StubGraph(),
                         web=web_provider)
    ctx = RoutingContext(kb_enabled=True, web_enabled=True, graph_ready=False)
    result = await engine.process("Godot 4.4 最新版本是什么", [], ctx)
    assert result.plan.channels == [Channel.WEB]
    assert "[联网]" in result.context_block
    assert "<untrusted_data" in result.context_block
    assert "docs.godotengine.org" in result.context_block


async def test_web_channel_failure_is_fail_soft():
    """联网挂了不影响主流程：通道降级空结果，整合渲染"（无结果）"。"""
    engine = make_engine(["search"], rag=StubRAG(), graph=StubGraph(),
                         web=BoomWeb())
    ctx = RoutingContext(web_enabled=True, kb_enabled=True, graph_ready=False)
    result = await engine.process("Godot 最新版本", [], ctx)
    assert "[联网]（无结果）" in result.context_block
    stat = [s for s in result.trace["channels"] if s["channel"] == "web"][0]
    assert stat["ok"] is False


async def test_all_off_llm_direct_note():
    """全关 → 明示直答（答案可能不含私有文档）——信任优先。"""
    engine = make_engine(["knowledge"])
    ctx = RoutingContext(kb_enabled=False, graph_ready=False, web_enabled=False)
    result = await engine.process("Area2D 是什么", [], ctx)
    assert result.plan.channels == [Channel.LLM_DIRECT]
    assert "未启用知识检索" in result.context_block


async def test_rag_doc_dedup_across_channels():
    """同一文档不重复注入（RAG 去重纪律 §1.4 易错点②）。"""
    dup = [_hit("doc1", "信号 A 的说明。"), _hit("doc1", "信号 A 的重复说明。")]
    engine = make_engine(["knowledge"], rag=StubRAG(hits=dup))
    ctx = RoutingContext(kb_enabled=True, graph_ready=False)
    result = await engine.process("信号 A 是什么", [], ctx)
    assert result.context_block.count("信号 A 的说明") == 1


async def test_hyde_switch_changes_rag_query_only():
    """HyDE 开关：RAG 检索句换成伪文档，图谱仍用原句。"""
    rag = StubRAG()
    graph = StubGraph()
    hyde_llm = FakeLLM(["伪文档：body_entered 在监测开启时触发。"])
    engine = make_engine(["knowledge"], rag=rag, graph=graph)
    engine.rewriter = QueryRewriter(hyde_llm, model="m")
    engine.hyde_enabled = True
    ctx = RoutingContext(kb_enabled=True, graph_ready=True, project_id="p")
    result = await engine.process("碰撞了没反应", [], ctx)
    assert rag.queries[0].startswith("伪文档")
    assert graph.queries[0] == "碰撞了没反应"
    assert result.trace["hyde"].startswith("伪文档")


async def test_trace_covers_five_segments():
    """trace 五段决策：意图/改写/路由 reason/通道耗时/注入预算全可见。"""
    engine = make_engine(["knowledge"])
    result = await engine.process("Area2D 的信号有哪些", [], CTX)
    t = result.trace
    assert t["intent"] == "knowledge"
    assert "rewritten" in t and "reason" in t
    assert isinstance(t["channels"], list) and t["channels"][0]["ms"] >= 0
    assert "inject_tokens" in t and t["elapsed_ms"] >= 0


async def test_rag_messages_appends_cite_prompt():
    """M03 接线：rag_messages 把注入块变 system 消息，RAG 命中带引用纪律。"""
    engine = make_engine(["knowledge"])
    result = await engine.process("Area2D 的信号", [], CTX)
    msgs = rag_messages(result)
    assert len(msgs) == 1 and msgs[0].role == "system"
    assert "<retrieved_context" in msgs[0].content
    assert "标注依据" in msgs[0].content
    # 空块（闲聊/craft）不注入
    chatty = await make_engine(["chitchat"]).process("谢谢", [], CTX)
    assert rag_messages(chatty) == []
