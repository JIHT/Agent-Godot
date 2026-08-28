"""M10 §4 步骤 7：引用渲染与抽取。"""
import pytest

from agent_godot.rag import (CITE_PROMPT, Chunk, CitationFormatter,
                              RetrievalHit)


def _hit(seq: int, source: str = "docs/physics.md", heading: str = "信号") -> RetrievalHit:
    return RetrievalHit(
        chunk=Chunk(text=f"chunk {seq}", source=source, heading=heading,
                    start=10 + seq, doc_id="d", kind="md", seq=seq),
        score=0.5, from_={"vec"})


def test_render_context_format():
    fmt = CitationFormatter()
    out = fmt.render_context([_hit(0), _hit(1)])
    assert out.startswith("[1] (docs/physics.md#信号)\nchunk 0")
    assert "[2] (docs/physics.md#信号)\nchunk 1" in out


def test_render_context_empty():
    assert CitationFormatter().render_context([]) == ""


def test_render_context_heading_fallback():
    """heading 为空时显示 '-'（引用锚点不悬空）。"""
    fmt = CitationFormatter()
    out = fmt.render_context([_hit(0, heading="")])
    assert "(docs/physics.md#-)" in out


def test_extract_citations():
    fmt = CitationFormatter()
    assert fmt.extract_citations("依据[1]与[3]，另见 [10]。") == [1, 3, 10]
    assert fmt.extract_citations("无引用") == []
    assert fmt.extract_citations("数组 a[0] 下标") == [0]   # 下标也认（宽松抽取）


def test_extract_citations_streaming_tolerant():
    """流式输出：编号先于论断出现也能抽（只认编号不认位置）。"""
    fmt = CitationFormatter()
    assert fmt.extract_citations("[2] 该结论的依据。") == [2]


def test_resolve_all_valid():
    """§5 验收：抽取回答里全部 [n]，均存在于注入 chunk 集。"""
    fmt = CitationFormatter()
    hits = [_hit(i) for i in range(4)]
    valid, dangling = fmt.resolve("回答 [1]，再看 [3]。", hits)
    assert valid == [1, 3]
    assert dangling == []


def test_resolve_detects_dangling_citation():
    """悬空编号 = 幻觉实锤（引用对不上的事后检测接口）。"""
    fmt = CitationFormatter()
    hits = [_hit(i) for i in range(2)]
    valid, dangling = fmt.resolve("编造了 [5] 的依据", hits)
    assert valid == []
    assert dangling == [5]


def test_cite_prompt_content():
    assert "[编号]" in CITE_PROMPT
    assert "知识库未覆盖" in CITE_PROMPT
    assert "禁止编造" in CITE_PROMPT
