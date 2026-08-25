"""tools/builtin/diff_tool.py —— Diff 生成 + 逐 hunk 应用器（M04 §4 步骤 7 / M06 §1.3 §4 步骤 8）

生成侧：标准库 difflib.unified_diff（LCS 算法了解即可）。
应用侧：结构化 Hunk 对象 + apply_hunks 逐块审批应用器——批准 3 块中的 2 块，
应用器要聪明地跳过没批的那块并把后面块的行号对齐（Cursor review 模式的核心体验）。

行号铁律（off-by-one 坟场）：hunk 头 @@ -5,3 @@ 是 **1-based** 行号，
数组换算要 -1；空文件/末尾无换行（\\ No newline）单独处理。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..registry import BaseTool, register_tool
from ..response import Artifact, ToolResponse

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    """一个 diff 块：旧/新起始行（1-based）与跨度，及块内旧/新行内容。"""
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)


def parse_hunks(diff_text: str) -> list[Hunk]:
    """把 unified diff 文本解析成 Hunk 列表（---/+++ 文件头被忽略）。"""
    hunks: list[Hunk] = []
    cur: Hunk | None = None
    for line in diff_text.splitlines():
        m = _HUNK_HEADER.match(line)
        if m:
            if cur:
                hunks.append(cur)
            o_start, o_count, n_start, n_count = m.groups()
            cur = Hunk(int(o_start), int(o_count or "1"),
                       int(n_start), int(n_count or "1"))
            continue
        if cur is None:
            continue                        # 文件头（--- old / +++ new）
        if line.startswith("\\"):           # "\ No newline at end of file"
            continue
        if line.startswith("+"):
            cur.new.append(line[1:])
        elif line.startswith("-"):
            cur.old.append(line[1:])
        elif line.startswith(" "):
            cur.old.append(line[1:])
            cur.new.append(line[1:])
        # 空行（无前缀空格）不是合法 unified diff 内容，忽略
    if cur:
        hunks.append(cur)
    return hunks


def apply_hunks(original: list[str], hunks: list[Hunk],
                approved: set[int]) -> list[str]:
    """逐块审批应用：approved 是被批准的 hunk 下标集合（按 hunks 列表顺序）。

    实现：用"源指针"替代 §1.3 ③ 的 offset 累计（重写干净版）——src 始终指向
    原文中已消费的位置，跳过的块原样保留原文行，后续块按原文行号对齐，
    不再有 off-by-one 坟场。跳过的块同样改变后续对齐——这里由指针天然保证。
    """
    out: list[str] = []
    src = 0                                # 原文已消费到的行（0-based）
    for idx, h in sorted(enumerate(hunks), key=lambda p: p[1].old_start):
        start = h.old_start - 1            # 1-based → 0-based
        if start < src:
            continue                       # 重叠块防御性跳过
        out.extend(original[src:start])    # 块前未变区域
        out.extend(h.new if idx in approved else h.old)
        src = start + len(h.old)
    out.extend(original[src:])             # 尾部未变区域
    return out


def apply_unified(old_text: str, diff_text: str, approved: set[int]) -> str:
    """便捷入口：旧文本 + diff + 批准集合 → 新文本（保留旧文本的末尾换行习惯）。"""
    hunks = parse_hunks(diff_text)
    result = apply_hunks(old_text.splitlines(), hunks, approved)
    text = "\n".join(result)
    if old_text.endswith("\n") and result:
        text += "\n"
    return text


@register_tool(name="diff", readonly=True, risk="low", tags={"util"})
class DiffTool(BaseTool):
    """对比两段文本的差异，输出 unified diff 格式（新文本相对旧文本的改动）。"""
    class Params(BaseModel):
        old_text: str = Field(description="修改前的文本")
        new_text: str = Field(description="修改后的文本")
        fromfile: str = Field(default="old", description="旧文件名标记")
        tofile: str = Field(default="new", description="新文件名标记")

    async def run(self, old_text: str, new_text: str,
                  fromfile: str = "old", tofile: str = "new") -> ToolResponse:
        diff = "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=fromfile, tofile=tofile))
        if not diff:
            return ToolResponse(ok=True, summary="(两段文本相同，无差异)")
        return ToolResponse(
            ok=True, summary=diff,
            data={"changed_lines": diff.count("\n@@")},
            artifacts=[Artifact(type="diff", ref="inline")])
