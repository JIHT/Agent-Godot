"""M10 §4 步骤 3：解析层——统一 ParsedDoc + 分发。"""
import httpx
import pytest

from agent_godot.rag import (GDScriptParser, HTMLParser, MarkdownParser,
                             ParsedDoc, URLParser, get_parser)

from .conftest import GODOT_MD


# ---------------------------------------------------------------- Markdown

def test_markdown_frontmatter_and_headings(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("---\ntitle: 物理手册\n---\n\n# A\n\n正文一\n\n## B\n\n正文二\n",
                 encoding="utf-8")
    doc = MarkdownParser().parse(p)
    assert doc.kind == "md"
    assert doc.title == "物理手册"
    assert doc.text.startswith("# A")            # frontmatter 已剥离
    assert doc.headings[0] == (1, "A")
    assert [t for _, t in doc.headings] == ["A", "B"]


def test_markdown_title_falls_back_to_first_h1(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("# 首个标题\n\n正文", encoding="utf-8")
    doc = MarkdownParser().parse(p)
    assert doc.title == "首个标题"


def test_doc_id_stable_per_source():
    """doc_id = hash(source)：改内容重灌 id 不变（增量更新的键）。"""
    d1 = ParsedDoc.make(source="docs/a.md", kind="md", text="v1")
    d2 = ParsedDoc.make(source="docs/a.md", kind="md", text="v2 完全不同")
    d3 = ParsedDoc.make(source="docs/b.md", kind="md", text="v1")
    assert d1.doc_id == d2.doc_id
    assert d1.doc_id != d3.doc_id


# ---------------------------------------------------------------- HTML/URL

def test_html_parser_extracts_text_and_headings(tmp_path):
    p = tmp_path / "page.html"
    p.write_text(
        "<html><head><title>Godot 文档</title>"
        "<script>alert('noise')</script></head><body>"
        "<h1>物理</h1><p>move_and_slide 返回布尔。</p>"
        "<h2>信号</h2><nav>导航噪声</nav><p>body_entered 触发。</p>"
        "</body></html>", encoding="utf-8")
    doc = HTMLParser().parse(p)
    assert doc.kind == "html"
    assert doc.title == "Godot 文档"
    assert "move_and_slide 返回布尔" in doc.text
    assert "alert" not in doc.text             # script 整块丢弃
    assert "导航噪声" not in doc.text          # nav 丢弃
    assert "# 物理" in doc.text                # h1 → Markdown 标记（保 H2 结构）
    assert doc.headings and doc.headings[0] == (1, "物理")


def test_url_parser_with_mock_transport():
    html = ("<h1>Godot</h1><p>CharacterBody2D 文档页。</p>")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, text=html)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    doc = URLParser(client=client).parse("https://docs.godot.test/physics")
    assert doc.kind == "url"
    assert doc.source == "https://docs.godot.test/physics"
    assert captured["url"] == "https://docs.godot.test/physics"
    assert "CharacterBody2D" in doc.text
    assert doc.headings and doc.headings[0] == (1, "Godot")


def test_url_parser_raises_on_http_error():
    def handler(request):
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        URLParser(client=client).parse("https://docs.godot.test/missing")


# ---------------------------------------------------------------- GDScript / 分发

def test_gdscript_parser_raw_passthrough(tmp_path):
    p = tmp_path / "player.gd"
    p.write_text("extends Node\n\nfunc ready():\n\tprint(1)\n", encoding="utf-8")
    doc = GDScriptParser().parse(p)
    assert doc.kind == "gdscript"
    assert doc.text.startswith("extends Node")


def test_get_parser_dispatch(tmp_path):
    assert isinstance(get_parser("md"), MarkdownParser)
    assert isinstance(get_parser("html"), HTMLParser)
    assert isinstance(get_parser("gdscript"), GDScriptParser)
    assert isinstance(get_parser("pdf").__class__, type)    # PDFParser（pypdf 懒加载）
    # 扩展名推断
    assert isinstance(get_parser(source="a/b/doc.md"), MarkdownParser)
    assert isinstance(get_parser(source="src/player.gd"), GDScriptParser)
    assert isinstance(get_parser(source="no_ext"), MarkdownParser)   # 默认 md
    with pytest.raises(ValueError):
        get_parser()


def test_get_parser_unknown_kind():
    with pytest.raises(ValueError):
        get_parser(kind="docx")   # type: ignore[arg-type]
