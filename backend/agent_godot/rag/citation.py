"""rag/citation.py —— 引用渲染与抽取（M10 §1.5 / §4 步骤 7）

没有引用的回答等于"我听说"（与幻觉不可区分），带 [n] 的回答是"文献可查"
——引用把模型观点升级为可验证主张，信任的基石；也是幻觉的事后检测接口
（引用对不上 = 幻觉实锤）。
"""
from __future__ import annotations

import re

from .retrieval import RetrievalHit

# 提示约束：与 render_context 的 [n] 编号体系配套（M12/M13 注入 system 用）
CITE_PROMPT = ("回答时必须标注依据：论断后附 [编号]；多个依据并列写出；"
               "检索结果不足以回答时明确说'知识库未覆盖'，禁止编造。")

_CITE = re.compile(r"\[(\d+)\]")


class CitationFormatter:
    """注入格式渲染 + 回答引用抽取 + 悬空检测。"""

    def render_context(self, hits: list[RetrievalHit]) -> str:
        """检索结果 → 注入上下文：[i] (source#heading) + 正文。

        编号即引用锚点——模型引用 [i] 即可溯源到 chunk。
        """
        return "\n\n".join(
            f"[{i}] ({h.chunk.source}#{h.chunk.heading or '-'})\n{h.chunk.text}"
            for i, h in enumerate(hits, 1))

    def extract_citations(self, answer: str) -> list[int]:
        """抽取回答里全部 [n]。

        流式输出容忍乱序（模型先写 [1] 再写论断——只认编号不认位置）。
        """
        return [int(n) for n in _CITE.findall(answer)]

    def resolve(self, answer: str,
                hits: list[RetrievalHit]) -> tuple[list[int], list[int]]:
        """验收（§5）：回答引用的编号是否都能落到注入的 chunk 集。

        返回 (有效编号, 悬空编号)——悬空 = 引用了不存在的依据 = 幻觉实锤。
        """
        valid = set(range(1, len(hits) + 1))
        cited = set(self.extract_citations(answer))
        return sorted(cited & valid), sorted(cited - valid)


__all__ = ["CITE_PROMPT", "CitationFormatter"]
