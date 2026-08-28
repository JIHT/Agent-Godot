"""rag/parsers —— 解析层：异构来源统一吐 ParsedDoc（M10 §1.1 / §4 步骤 3）

大白话：图书馆的"采购编目"。不管书是纸的（PDF）、笔记（Markdown）、
网页（URL/HTML）还是代码（.gd），进馆前先统一登记成一张卡片（ParsedDoc）：
正文 + 标题树 + 来源信息。后面的切分/嵌入/检索只认卡片，不认原格式。

铁律：chunk 的引用质量在解析时就决定了——source/heading/页码没提取，
后面检索命中也无法溯源（garbage in, garbage out 的第一站，§1.1 易错点）。

doc_id = hash(source)：增量更新的键是"来源"而非"内容"——
用户改了文档重灌时 doc_id 不变，才能删旧 chunk 不残留（§3）。
"""
from __future__ import annotations

import hashlib
import html as html_mod
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import httpx

logger = logging.getLogger(__name__)

ParserKind = Literal["pdf", "md", "html", "url", "gdscript"]


# ---------------------------------------------------------------- ParsedDoc

@dataclass
class ParsedDoc:
    """解析产物：统一卡片。

    - text：规整后的纯文本（Markdown 保留 # 标记——切分器要按 H2 切节）
    - headings：标题树 [(level, text)]（level 1~6）
    - metadata：来源特有信息（pages / url / lines …），随 chunk 落库
    """
    doc_id: str                       # ★ 稳定 id = hash(source)，增量更新的键
    source: str                       # 来源标识：相对路径 / URL（正斜杠统一）
    kind: ParserKind                  # 决定切分策略（gdscript 走符号边界）
    text: str                         # 全文
    title: str = ""
    headings: list[tuple[int, str]] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @staticmethod
    def make(source: str, kind: ParserKind, text: str, title: str = "",
             headings: list[tuple[int, str]] | None = None,
             metadata: dict | None = None) -> "ParsedDoc":
        doc_id = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
        return ParsedDoc(doc_id=doc_id, source=source, kind=kind, text=text,
                         title=title, headings=headings or [],
                         metadata=metadata or {})


class Parser(Protocol):
    """解析器协议：一切来源 → ParsedDoc。"""
    def parse(self, source: Path | str) -> ParsedDoc: ...


# ---------------------------------------------------------------- 公共工具

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


def scan_md_headings(text: str) -> list[tuple[int, int, str]]:
    """扫描 Markdown 标题行 → [(行号, level, 标题)]（切分器做面包屑用）。"""
    out: list[tuple[int, int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if m:
            out.append((i, len(m.group(1)), m.group(2).strip()))
    return out


def _split_frontmatter(raw: str) -> tuple[str, str]:
    """YAML frontmatter → (title, body)。只取 title（教学版不引 markdown 库）。"""
    m = _FRONTMATTER.match(raw)
    if not m:
        return "", raw
    title = ""
    for line in m.group(1).splitlines():
        if line.startswith("title:"):
            title = line.split(":", 1)[1].strip().strip("\"'")
            break
    return title, raw[m.end():]


# 脚本/style/nav 等整块丢弃（正文抽取的噪声源）
_DROP_BLOCK = re.compile(
    r"<(script|style|noscript|nav|header|footer|aside|iframe|svg)[^>]*>.*?</\1>",
    re.I | re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HEADING_TAG = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.I | re.S)
_BLOCK_TAG = re.compile(
    r"</?(?:p|div|li|tr|td|th|pre|br|table|section|article|blockquote|ul|ol|h[1-6])"
    r"[^>]*/?>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def html_to_text(html: str) -> tuple[str, str, list[tuple[int, int, str]]]:
    """HTML → (正文, title, 标题树)。

    标题标签转 Markdown # 标记（保住 H2 结构——结构感知切分靠它），
    块级标签转换行，剥剩余标签后解实体。教学级实现（不引 readability）。
    """
    m = _TITLE_TAG.search(html)
    title = _ANY_TAG.sub("", m.group(1)).strip() if m else ""

    def _h(match: re.Match) -> str:
        level = int(match.group(1))
        text = _ANY_TAG.sub("", match.group(2)).strip()
        return f"\n\n{'#' * level} {text}\n\n"

    text = _DROP_BLOCK.sub(" ", html)
    text = _COMMENT.sub(" ", text)
    text = _HEADING_TAG.sub(_h, text)
    text = _BLOCK_TAG.sub("\n", text)
    text = _ANY_TAG.sub("", text)
    text = html_mod.unescape(text)

    # 行规整：去行尾空白 + 压缩连续空行（保留一个空行 = 段落边界）
    out: list[str] = []
    blank = 0
    for ln in text.splitlines():
        ln = ln.rstrip()
        blank = blank + 1 if not ln.strip() else 0
        if blank <= 1:
            out.append(ln)
    body = "\n".join(out).strip()
    return body, title, scan_md_headings(body)


# ---------------------------------------------------------------- 解析器

class _FileParser:
    """本地文件解析基类：读文件 → kind 特定规整 → ParsedDoc。"""
    kind: ParserKind = "md"

    def parse(self, source: Path | str) -> ParsedDoc:
        p = Path(source)
        raw = p.read_text(encoding="utf-8", errors="replace")
        source_str = str(p).replace("\\", "/")
        text, title, headings, metadata = self._refine(raw)
        return ParsedDoc.make(source=source_str, kind=self.kind, text=text,
                              title=title, headings=headings,
                              metadata=metadata)

    def _refine(self, raw: str
                ) -> tuple[str, str, list[tuple[int, int, str]], dict]:
        raise NotImplementedError


class MarkdownParser(_FileParser):
    """Markdown：frontmatter title 提取 + 标题树扫描（正文原样保留）。"""
    kind: ParserKind = "md"

    def _refine(self, raw: str):
        title, body = _split_frontmatter(raw)
        headings = [(lv, t) for _, lv, t in scan_md_headings(body)]
        if not title and headings:
            title = headings[0][1]        # 兜底：首个 H1 当标题
        return body, title, headings, {"chars": len(body)}


class HTMLParser(_FileParser):
    """本地 HTML：正文抽取（标题标签转 Markdown 标记保结构）。"""
    kind: ParserKind = "html"

    def _refine(self, raw: str):
        body, title, headings3 = html_to_text(raw)
        # html_to_text 返回 [(行号, level, title)] 三元组；ParsedDoc.headings 用 (level, title)
        headings = [(lv, t) for _, lv, t in headings3]
        return body, title, headings, {"chars": len(body)}


class GDScriptParser(_FileParser):
    """GDScript 源码：原文直通（符号边界切分是 chunking/structure_aware 的活）。"""
    kind: ParserKind = "gdscript"

    def _refine(self, raw: str):
        return raw, "", [], {"lines": raw.count("\n") + 1}


class URLParser:
    """URL：httpx 拉取 → 正文抽取（client 可注入，测试用 MockTransport）。"""
    kind: ParserKind = "url"

    def __init__(self, timeout: float = 30.0, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=timeout, follow_redirects=True)

    def parse(self, source: Path | str) -> ParsedDoc:
        url = str(source)
        resp = self._client.get(url)
        resp.raise_for_status()
        body, title, headings = html_to_text(resp.text)
        return ParsedDoc.make(source=url, kind="url", text=body, title=title,
                              headings=[(lv, t) for _, lv, t in headings],
                              metadata={"url": url, "chars": len(body)})


class PDFParser:
    """PDF：pypdf 逐页抽取，页码入 metadata（深坑见 §1.5：复杂 PDF 建议转 Markdown）。"""
    kind: ParserKind = "pdf"

    def parse(self, source: Path | str) -> ParsedDoc:
        try:
            from pypdf import PdfReader       # 懒 import：无 pypdf 不拖垮包加载
        except ImportError as e:
            raise ImportError(
                "PDF 解析需要 pypdf：uv add pypdf（扫描件/双栏/表格是深坑，"
                "复杂 PDF 建议先转 Markdown）") from e
        path = str(source)
        reader = PdfReader(path)
        pages: list[str] = []
        for i, page in enumerate(reader.pages, 1):
            t = (page.extract_text() or "").strip()
            if t:
                pages.append(f"[page {i}]\n{t}")
        text = "\n\n".join(pages)
        return ParsedDoc.make(source=path.replace("\\", "/"), kind="pdf",
                              text=text, metadata={"pages": len(reader.pages)})


# ---------------------------------------------------------------- 分发

_EXT_KIND = {".md": "md", ".markdown": "md", ".txt": "md",
             ".html": "html", ".htm": "html", ".pdf": "pdf",
             ".gd": "gdscript", ".url": "url"}


def get_parser(kind: ParserKind | None = None, *,
               source: str | Path | None = None,
               http_client: httpx.Client | None = None) -> Parser:
    """按 kind（显式优先）或 source 扩展名分发解析器（§4 步骤 3）。"""
    if kind is None:
        if source is None:
            raise ValueError("kind 与 source 至少传一个")
        kind = _EXT_KIND.get(Path(source).suffix.lower(), "md")
    match kind:
        case "md":
            return MarkdownParser()
        case "html":
            return HTMLParser()
        case "gdscript":
            return GDScriptParser()
        case "url":
            return URLParser(client=http_client)
        case "pdf":
            return PDFParser()
        case _:
            raise ValueError(f"未知解析类型: {kind!r}")


__all__ = ["GDScriptParser", "HTMLParser", "MarkdownParser", "PDFParser",
           "ParsedDoc", "Parser", "ParserKind", "URLParser", "get_parser",
           "html_to_text", "scan_md_headings"]
