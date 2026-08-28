"""rag/chunking/recursive.py —— 递归切分（默认策略，M10 §1.1 ③ / §4 步骤 4）

分隔符优先级从高到低（语义边界 → 段落 → 句子 → 词 → 硬切）：
本级能切且每块不超限 → 返回；仍有超长块 → 降级用更细的分隔符继续切；
所有分隔符都不在文本里 → hard_split 兜底（唯一带 overlap 的路径——
语义边界失败时的最后手段，太薄/太厚的折中见 §1.1 面试 1）。
"""
from __future__ import annotations

import re

from ..parsers import ParsedDoc, scan_md_headings
from . import Chunk, Chunker

SEPARATORS = ["\n\n## ", "\n\n### ", "\n\n", "\n", "。", ". ", " ", ""]


def hard_split(text: str, max_len: int, overlap: int = 0) -> list[str]:
    """兜底硬切 + 重叠窗口（每片带一点上一片的边，防关键句恰好断在切分点）。"""
    if len(text) <= max_len:
        return [text]
    step = max(max_len - overlap, 1)
    return [text[i:i + max_len] for i in range(0, len(text), step)]


def recursive_split(text: str, max_len: int, overlap: int = 0) -> list[str]:
    """递归切分：优先语义边界，超长块降级，最终 hard_split 兜底。"""
    if len(text) <= max_len:
        return [text]
    for sep in SEPARATORS:
        if not sep:
            continue            # ★ "" 兜底项：in 恒真但 split("") 会崩，跳过走 hard_split
        if sep not in text:
            continue
        parts = text.split(sep)
        chunks, buf = [], ""
        for p in parts:
            candidate = (buf + sep + p) if buf else p
            if len(candidate) > max_len and buf:
                chunks.append(buf)               # 满了就出块
                buf = p
            else:
                buf = candidate
        if buf:
            chunks.append(buf)
        # 超长块降级：用更细的分隔符继续切（真·递归）
        out: list[str] = []
        for c in chunks:
            if len(c) > max_len:
                out.extend(recursive_split(c, max_len, overlap))
            else:
                out.append(c)
        return out
    return hard_split(text, max_len, overlap)


def breadcrumb(headings: list[tuple[int, int, str]], line: int) -> str:
    """行号 → 所在标题的面包屑路径（标题栈按 level 弹压）。

    "Vector math > Advanced > Dot product"——chunk 自带定位信息，
    contextual chunking 的零成本轻量版（§7 面试 2）。
    """
    stack: list[tuple[int, str]] = []
    for h_line, level, title in headings:
        if h_line > line:
            break
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
    return " > ".join(t for _, t in stack)


def _approx_start_line(text: str, line: int, piece: str) -> int:
    """下一个块的近似起始行（分隔符被 split 消耗，行号会缓慢漂移——可接受）。"""
    return line + piece.count("\n") + 1


class RecursiveChunker(Chunker):
    """递归切分器（默认）：整篇 recursive_split + 面包屑回填。"""

    def __init__(self, max_len: int = 1200, overlap: int = 128):
        self.max_len = max_len        # 字符数近似 token 预算（中英混合折中）
        self.overlap = overlap

    def split(self, doc: ParsedDoc) -> list[Chunk]:
        headings = scan_md_headings(doc.text)
        pieces = recursive_split(doc.text, self.max_len, self.overlap)
        chunks: list[Chunk] = []
        line = 1
        for seq, piece in enumerate(pieces):
            chunks.append(Chunk(
                text=piece, source=doc.source, kind=doc.kind,
                heading=breadcrumb(headings, line), start=line,
                doc_id=doc.doc_id, seq=seq))
            line = _approx_start_line(doc.text, line, piece)
        return chunks


__all__ = ["RecursiveChunker", "SEPARATORS", "breadcrumb",
           "hard_split", "recursive_split"]
