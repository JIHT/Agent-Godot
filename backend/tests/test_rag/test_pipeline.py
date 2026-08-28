"""M10 §5 验收③④：增量建库（旧 chunk 不残留）+ 三存储对账 + 引用闭环。"""
import pytest

from agent_godot.rag import (BM25Index, CitationFormatter, FakeEmbeddingService,
                             HybridRetriever, InMemoryVectorIndex,
                             IngestPipeline, ParsedDoc, Reranker,
                             StructureAwareChunker)


def _pipeline(embedder, vec=None, bm25=None) -> IngestPipeline:
    return IngestPipeline(
        chunker=StructureAwareChunker(max_len=300),
        embedder=embedder, vec=vec, bm25=bm25)


async def test_ingest_then_retrieve_md(embedder, vec, bm25, retriever):
    pipe = _pipeline(embedder, vec, bm25)
    from .conftest import GODOT_MD
    doc = ParsedDoc.make(source="docs/physics.md", kind="md", text=GODOT_MD)
    n = await pipe.upsert_document(doc)
    assert n >= 3                       # 前言 + move_and_slide 节 + Area2D 信号节(+碰撞报告)

    hits = await retriever.retrieve("body_entered 信号什么时候触发", top_k=3)
    assert hits
    assert any("body_entered" in h.chunk.text for h in hits)


async def test_upsert_replaces_old_chunks(embedder, vec, bm25):
    """§5 验收③：doc 改后重灌——chunk 数量变化、旧 heading 检索不到。"""
    pipe = _pipeline(embedder, vec, bm25)
    from .conftest import GODOT_MD
    v1 = ParsedDoc.make(source="docs/physics.md", kind="md", text=GODOT_MD)
    n1 = await pipe.upsert_document(v1)

    # 改文档：删掉 Area2D 节、加一节新内容
    v2_text = GODOT_MD.split("## Area2D 信号")[0] + \
        "## 新增章节\n\nNavigationAgent2D 的 navigation_finished 信号在到达终点时触发。\n"
    v2 = ParsedDoc.make(source="docs/physics.md", kind="md", text=v2_text)
    n2 = await pipe.upsert_document(v2)
    # ★ 旧 chunk 不残留（核心验收）：三存储都查不到 body_entered
    assert not pipe.bm25.search("body_entered", top=5)
    vec_hits = await pipe.vec.search(await embedder.embed_query("body_entered"), top=10)
    assert "body_entered" not in {c.text for c, _ in vec_hits}
    meta_chunks = await pipe.db.get_chunks(v2.doc_id)
    assert all("body_entered" not in c.text for c in meta_chunks)
    # 新内容可检索
    top_id, _ = pipe.bm25.search("navigation_finished", top=1)[0]
    assert "navigation_finished" in pipe.bm25.get_chunk(top_id).text


async def test_audit_detects_drift(embedder, vec, bm25):
    """对账：人为制造三存储漂移 → audit() 报告。"""
    pipe = _pipeline(embedder, vec, bm25)
    from .conftest import GODOT_MD
    doc = ParsedDoc.make(source="docs/physics.md", kind="md", text=GODOT_MD)
    await pipe.upsert_document(doc)
    assert await pipe.audit() == {}     # 正常灌库后三处一致

    # 人为删 Milvus（模拟"先删后插"中途崩溃的旧态残留）
    vec.delete_doc(doc.doc_id)
    drift = await pipe.audit()
    assert doc.doc_id in drift
    assert drift[doc.doc_id]["vec"] == 0
    assert drift[doc.doc_id]["meta"] > 0

    # 重灌即愈（§3：最坏情况是多删，重建一次即愈）
    await pipe.upsert_document(doc)
    assert await pipe.audit() == {}


async def test_ingest_two_docs_isolated(embedder, vec, bm25):
    """多文档：doc_id 隔离删除。"""
    pipe = _pipeline(embedder, vec, bm25)
    d1 = ParsedDoc.make(source="a.md", kind="md",
                        text="# A\n\n## 甲\n\nmax_contacts_reported 说明。")
    d2 = ParsedDoc.make(source="b.md", kind="md",
                        text="# B\n\n## 乙\n\nmove_and_slide 说明。")
    await pipe.upsert_document(d1)
    await pipe.upsert_document(d2)
    # 多文档：两 doc 都进了三存储，且按 doc_id 隔离删除
    n1_meta = await pipe.db.count(d1.doc_id)
    n2_meta = await pipe.db.count(d2.doc_id)
    assert n1_meta == n2_meta == 2          # 每文档 H1+H2 两节
    assert vec.count(d1.doc_id) == 2
    assert bm25.count(d2.doc_id) == 2
    assert await pipe.audit() == {}          # 三处一致


async def test_answer_citations_all_resolvable(embedder, vec, bm25, retriever):
    """§5 验收④：抽取回答里全部 [n]，均存在于注入 chunk 集。"""
    pipe = _pipeline(embedder, vec, bm25)
    from .conftest import GODOT_MD
    await pipe.upsert_document(
        ParsedDoc.make(source="docs/physics.md", kind="md", text=GODOT_MD))

    query = "Area2D 的 body_entered 信号在什么条件下触发？"
    hits = await retriever.retrieve(query, top_k=5)
    assert hits

    fmt = CitationFormatter()
    context = fmt.render_context(hits)
    assert "[1] (" in context

    # 模拟模型带引用的回答（编号均在 1..len(hits) 内）
    answer = ("body_entered 在监测双方都启用时触发 [1]。"
              "monitored 控制对方能否监测本节点 [1]。"
              "碰撞数上限由 max_contacts_reported 控制 [2]。")
    valid, dangling = fmt.resolve(answer, hits)
    assert dangling == []
    assert 1 in valid and 2 in valid


async def test_gd_script_ingest(embedder, vec, bm25):
    """.gd 建库：函数块完整可检索（结构感知链路端到端）。"""
    pipe = _pipeline(embedder, vec, bm25)
    from .conftest import PLAYER_GD
    doc = ParsedDoc.make(source="player.gd", kind="gdscript", text=PLAYER_GD)
    n = await pipe.upsert_document(doc)
    assert n >= 3                        # 文件头 + 两个函数块

    top_id, _ = pipe.bm25.search("_check_landing", top=1)[0]
    chunk = pipe.bm25.get_chunk(top_id)
    assert "Player._check_landing" in chunk.heading
    assert "# [player.gd · Player]" in chunk.text
