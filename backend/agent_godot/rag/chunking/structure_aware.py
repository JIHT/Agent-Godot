"""rag/chunking/structure_aware.py —— 结构感知切分（M10 §1.1 / §4 步骤 4）

大白话：顺着土豆纹理切（§1.1）。两大策略：
- .gd：按 M06 符号边界切（函数/信号块完整），chunk 头回填"文件 + 类名"
  上下文——脱离类的函数体是孤岛，类名回填让嵌入携带全局语境
- 文档（md/html/url/pdf）：按 H1/H2 切节，chunk 保留面包屑路径
  （"Vector math > Advanced > Dot product"）——检索命中后引用更准

超长节/超长函数内部再走递归切分兜底。
"""
from __future__ import annotations

import re

from ...tools.godot.script_tools import gd_symbols   # M06 符号解析（单一事实源）
from ..parsers import ParsedDoc, scan_md_headings
from . import Chunk, Chunker
from .recursive import (RecursiveChunker, breadcrumb, hard_split,
                        recursive_split)

# 顶层符号边界：func / signal / class 声明行（缩进 0——内层方法跟外层块走）
_GD_TOP_SYMBOL = re.compile(r"^(?:func\s+\w+|signal\s+\w+|class\s+\w+)")
_CLASS_NAME = re.compile(r"^class_name\s+(\w+)", re.M)


class StructureAwareChunker(Chunker):
    """结构感知切分：.gd 走符号边界 + 类名回填；文档走 H2 + 面包屑。"""

    def __init__(self, max_len: int = 1200, overlap: int = 128):
        self.max_len = max_len
        self.overlap = overlap
        self._recursive = RecursiveChunker(max_len, overlap)   # 节内兜底

    def split(self, doc: ParsedDoc) -> list[Chunk]:
        if doc.kind == "gdscript":
            return self._split_gd(doc)
        return self._split_doc(doc)

    # ---------------------------------------------------------------- .gd

    def _split_gd(self, doc: ParsedDoc) -> list[Chunk]:
        lines = doc.text.splitlines()
        n = len(lines)
        # M06 符号大纲：顶层 func/signal 做块边界（缩进 0——内层方法跟外层块走）；
        # class 声明行（M06 正则不覆盖）本地补充
        bounds = [ln for ln, kind, _ in gd_symbols(doc.text)
                  if kind in ("func", "signal")
                  and not lines[ln - 1][:1].isspace()]
        bounds += [i for i, ln in enumerate(lines, 1)
                   if _GD_TOP_SYMBOL.match(ln) and i not in bounds]
        bounds.sort()
        if not bounds:                       # 无顶层符号 → 递归兜底
            return self._recursive.split(doc)

        # 每个符号块起点上溯吸收前导注释（注释跟着函数走，不归上一块）
        starts = []
        for b in bounds:
            s = b
            while s > 1 and lines[s - 2].lstrip().startswith("#"):
                s -= 1
            starts.append(s)

        # 块划分：文件头（extends/class_name/变量区）+ 各符号块
        spans: list[tuple[int, int, str]] = []
        if starts[0] > 1:
            spans.append((1, starts[0] - 1, "file header"))
        for j, b in enumerate(bounds):
            end = (starts[j + 1] - 1) if j + 1 < len(starts) else n
            spans.append((starts[j], end, lines[b - 1]))

        class_name = _CLASS_NAME.search(doc.text)
        cls = class_name.group(1) if class_name else ""
        chunks: list[Chunk] = []
        for s, e, decl in spans:
            body = "\n".join(lines[s - 1:e])
            if not body.strip():
                continue
            # ★ 类名回填头部：零成本版 contextual chunking（§1.1 ②）
            ctx = f"# [{doc.source} · {cls or '(no class_name)'}]"
            name = self._symbol_name(decl)
            heading = f"{cls + '.' if cls else ''}{name}" if name != "file header" \
                else f"{cls or doc.source} (file header)"
            line = s
            for piece in self._pieces(f"{ctx}\n{body}"):
                chunks.append(Chunk(
                    text=piece, source=doc.source, kind=doc.kind,
                    heading=heading, start=line, doc_id=doc.doc_id,
                    seq=len(chunks)))
                line += piece.count("\n") + 1
        return chunks

    @staticmethod
    def _symbol_name(decl: str) -> str:
        m = re.match(r"^(?:func|signal|class)\s+(\w+)", decl)
        return m.group(1) if m else "file header"

    # ---------------------------------------------------------------- 文档

    def _split_doc(self, doc: ParsedDoc) -> list[Chunk]:
        text = doc.text
        lines = text.splitlines()
        headings = scan_md_headings(text)
        bounds = [(ln, lv, t) for ln, lv, t in headings if lv <= 2]
        if not bounds:                       # 无 H1/H2 → 递归兜底
            return self._recursive.split(doc)

        # 节划分：前言 + 每节（标题行到下一节标题行前）
        spans: list[tuple[int, int]] = []
        if bounds[0][0] > 1:
            spans.append((1, bounds[0][0] - 1))
        for j, (ln, _, _) in enumerate(bounds):
            end = (bounds[j + 1][0] - 1) if j + 1 < len(bounds) else len(lines)
            spans.append((ln, end))

        chunks: list[Chunk] = []
        for s, e in spans:
            seg = "\n".join(lines[s - 1:e])
            if not seg.strip():
                continue
            heading = breadcrumb(headings, s)   # H1 > H2（含本节标题）
            line = s
            for piece in self._pieces(seg):
                chunks.append(Chunk(
                    text=piece, source=doc.source, kind=doc.kind,
                    heading=heading, start=line, doc_id=doc.doc_id,
                    seq=len(chunks)))
                line += piece.count("\n") + 1
        return chunks

    # ---------------------------------------------------------------- 公共

    def _pieces(self, seg: str) -> list[str]:
        """节内再切：短节整块出，超长节递归切（H3 优先级天然在 SEPARATORS 里）。"""
        if len(seg) <= self.max_len:
            return [seg]
        return recursive_split(seg, self.max_len, self.overlap)


__all__ = ["StructureAwareChunker"]
