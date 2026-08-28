"""hooks/post_tool/format_hook.py —— .gd 落盘后自动格式化（M14 §4 步骤 3）

钩子存在的理由：模型写 GDScript 十次有三次缩进用空格（GDScript 强制 Tab，
空格缩进直接 parse error），而"每次都在系统提示里念一遍"既费 token 又不可靠。
规则化格式化放在 post_tool：模型看到的是"我写的东西被整理过了"的 Observation，
磁盘上是可编译的代码——**改的是现实，不是提示**。

做三件事（简单规则，不做完整语义格式化，那是 Godot 编辑器的活）：
① 行尾空白清除  ② 连续 4 空格缩进 → 1 个 Tab  ③ 连续空行压缩为 1、文末恰好一个换行

★ 乐观锁联动：格式化改了磁盘内容 → 工具返回的 hash 失效。hook 必须把新
hash 写回响应的 data 与 summary，否则模型下一次 write 必然 CONFLICT。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from agent_godot.tools import ToolResponse
from agent_godot.tools.file_lock import sha16
from agent_godot.tools.sandbox import (DeniedPathError, PathEscapeError,
                                       resolve_in_root)

from ..pipeline import HookContext, HookResult, HookSpec

# 触发文件后缀（Godot 生态里 .gd 对缩进最敏感；.tscn/.tres 是 Godot 自家格式，不动）
TARGET_SUFFIXES = {".gd"}

# 参数里可能装目标路径的键（不同工具命名不同，统一探测）
_PATH_KEYS = ("path", "file", "target", "script", "scene")


def format_gdscript(text: str, tab_width: int = 4) -> str:
    """GDScript 轻量格式化（纯函数，无 IO，好测）。

    三引号字符串块整体跳过（改缩进会改变字符串内容——格式化不该改语义）。
    """
    if not text:
        return ""
    newline = "\r\n" if "\r\n" in text else "\n"
    out: list[str] = []
    blank = 0
    in_triple = False
    for line in text.splitlines():
        if in_triple:                       # 多行字符串内部：原样保留
            out.append(line.rstrip())
            if '"""' in line:
                in_triple = False
            continue
        if line.count('"""') % 2 == 1:
            in_triple = True
        content = line.rstrip()
        if not content.strip():
            blank += 1
            if blank > 1:
                continue                    # 连续空行压成一个
            out.append("")
            continue
        blank = 0
        leading = content[:len(content) - len(content.lstrip())]
        indent = leading.replace(" " * tab_width, "\t")
        out.append(indent + content.strip())
    body = newline.join(out).rstrip("\r\n")
    return body + newline if body else ""


class FormatHook:
    """post_tool：写类工具落盘 .gd 后立即格式化（priority=100 业务类）。"""

    name = "format"
    PRIORITY = 100

    def __init__(self, root: Path, tools: set[str] | None = None,
                 max_bytes: int = 200_000):
        self.root = Path(root)
        self.tools = set(tools or ())       # 空 = 不限工具名（只看参数里的后缀）
        self.max_bytes = max_bytes          # 大文件不碰（30ms 预算）

    @property
    def point(self) -> str:
        return "post_tool"

    def spec(self) -> HookSpec:
        return HookSpec(name=self.name, point="post_tool",
                        priority=self.PRIORITY, handler=self)

    async def __call__(self, ctx: HookContext) -> HookResult | None:
        resp = ctx.response
        if resp is None or not resp.ok:
            return None                                  # 失败响应无需排版
        if self.tools and ctx.tool not in self.tools:
            return None
        path = _target_path(ctx.args)
        if not path or Path(path).suffix.lower() not in TARGET_SUFFIXES:
            return None
        try:
            abs_p = resolve_in_root(self.root, path)
        except (PathEscapeError, DeniedPathError):
            return None
        if not abs_p.is_file():
            return None
        try:
            if abs_p.stat().st_size > self.max_bytes:
                return None
            raw = await asyncio.to_thread(abs_p.read_text, encoding="utf-8")
        except (OSError, ValueError):
            return None

        formatted = format_gdscript(raw)
        if formatted == raw:
            return None                                  # 已经很干净：pass

        try:
            await asyncio.to_thread(abs_p.write_text, formatted, encoding="utf-8")
        except OSError:
            return None

        new_hash = sha16(formatted)
        note = (f"\n[format hook] {path} 已自动格式化（缩进转 Tab / 空行归一），"
                f"当前 hash: {new_hash}")
        new_resp = replace(resp, summary=resp.summary + note,
                           data=_with_hash(resp.data, new_hash))
        return HookResult.modify(response=new_resp, reason=f"格式化 {path}")


def _target_path(args: dict) -> str:
    for key in _PATH_KEYS:
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _with_hash(data, new_hash: str):
    """把新 hash 写回 data（乐观锁：模型下一轮 write 要用它当 expect_hash）。"""
    if isinstance(data, dict):
        merged = dict(data)
        merged["hash"] = new_hash
        return merged
    return data


__all__ = ["FormatHook", "TARGET_SUFFIXES", "format_gdscript"]
