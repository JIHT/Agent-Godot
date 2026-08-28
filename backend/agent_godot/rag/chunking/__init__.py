"""rag/chunking —— 切分层（M10 §1.1 / §4 步骤 4）

两条铁律：语义完整（不把一个函数/一节文档切两半）+ 长度适中
（目标 256~512 token；太短丢上下文，太长稀释相似度）。

- Chunk：正文 + 溯源四件套（source/heading/行号/doc_id）——
  ★ 没有元数据的 chunk 检索命中后无法引用溯源（§1.1 易错点）
- RecursiveChunker：递归分隔符切分（默认策略，2023 事实标准）
- StructureAwareChunker：结构感知（.gd 按符号边界 / 文档按 H2+面包屑）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..parsers import ParsedDoc


@dataclass
class Chunk:
    """一个 chunk：正文 + 溯源元数据。

    start 为起始行号（1-based，在 ParsedDoc.text 中）——递归切分因分隔符
    被消耗行号是近似值；结构感知切分按真实边界行号（精确）。
    """
    text: str
    source: str
    heading: str            # 面包屑（"Vector math > Advanced > Dot product"）
    start: int
    doc_id: str
    kind: str
    seq: int = 0            # 文档内序号（chunk_id = f"{doc_id}:{seq}"）

    @property
    def chunk_id(self) -> str:
        """两路检索共用的主键——id 对不齐 RRF 直接失效（§1.4 易错点）。"""
        return f"{self.doc_id}:{self.seq}"


class Chunker(ABC):
    """切分器基类：ParsedDoc → list[Chunk]。"""

    @abstractmethod
    def split(self, doc: ParsedDoc) -> list[Chunk]:
        ...


# 子模块 import 放在 Chunk/Chunker 定义之后，避免 from . import Chunk 的循环
from .recursive import (RecursiveChunker, breadcrumb, hard_split,   # noqa: E402
                        recursive_split)
from .structure_aware import StructureAwareChunker                     # noqa: E402

__all__ = ["Chunk", "Chunker", "RecursiveChunker", "StructureAwareChunker",
           "breadcrumb", "hard_split", "recursive_split"]
