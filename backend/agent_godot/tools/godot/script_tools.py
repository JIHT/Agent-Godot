"""tools/godot/script_tools.py —— 脚本域 FC 工具（M06 §4 步骤 5）

- read_script：带符号大纲（函数/信号/class_name + 行号），hash 附尾部
- write_script：复用 M04 OptimisticFileStore（乐观锁）+ 写前检查点 + 写后自动 check
- list_symbols：全项目符号表（模型跨文件找 API 用）

GDScript 只做缩进块的轻量解析（完整语义分析是 Godot 编辑器的活）。
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from ..registry import BaseTool
from ..response import ErrorKind, ToolError, ToolResponse
from ..sandbox import DENY_PARTS, DeniedPathError, PathEscapeError, resolve_in_root
from .scene_tools import GodotContext, _auto_check, _godot_tool

# 符号声明：class_name / signal / func（任意缩进——类内方法也算）
_GD_SYMBOL = re.compile(r"^[ \t]*(?P<kind>class_name|signal|func|extends)\s+(?P<name>\w+)")


def gd_symbols(text: str) -> list[tuple[int, str, str]]:
    """提取 GDScript 符号大纲：[(行号, kind, name), ...]。"""
    out: list[tuple[int, str, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        m = _GD_SYMBOL.match(line)
        if m:
            out.append((i, m.group("kind"), m.group("name")))
    return out


def _outline(text: str) -> str:
    rows = [f"  L{ln:>4} {kind} {name}" for ln, kind, name in gd_symbols(text)]
    return "\n".join(rows) if rows else "  （无 class_name/signal/func 声明）"


@_godot_tool("godot_read_script", readonly=True)
class GodotReadScriptTool(BaseTool):
    """读取 .gd 脚本内容（附符号大纲与版本 hash——write 时作 expect_hash 传回）。"""
    class Params(BaseModel):
        path: str = Field(description="脚本相对路径，如 'player.gd'")

    def __init__(self, ctx: GodotContext):
        self.ctx = ctx

    async def run(self, path: str) -> ToolResponse:
        try:
            content, h = await self.ctx.store.read(path)
        except (PathEscapeError, DeniedPathError) as e:
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.DENIED, tool="godot_read_script", message=str(e),
                hint="路径越出项目根，请用项目内相对路径"))
        if h == "":
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.NOT_FOUND, tool="godot_read_script",
                message=f"脚本不存在: {path}",
                hint="用 godot_write_script 创建（新文件 expect_hash 传空字符串）"))
        summary = (f"{content}\n\n--- 符号大纲:\n{_outline(content)}"
                   f"\n--- 当前版本 hash: {h}"
                   f"（godot_write_script 时作为 expect_hash 传入）")
        return ToolResponse(ok=True, summary=summary,
                            data={"hash": h, "length": len(content)})


@_godot_tool("godot_write_script", readonly=False, risk="medium")
class GodotWriteScriptTool(BaseTool):
    """写入 .gd 脚本（乐观锁）：必须传 read 返回的 hash，新文件传空字符串。
    写前自动检查点快照（可回滚），写后自动触发 L1 语法校验。"""
    class Params(BaseModel):
        path: str = Field(description="目标脚本相对路径")
        content: str = Field(description="完整的新脚本内容（整体覆盖）")
        expect_hash: str = Field(default="",
                                 description="godot_read_script 返回的版本 hash；新文件传空字符串")

    def __init__(self, ctx: GodotContext):
        self.ctx = ctx

    async def run(self, path: str, content: str,
                  expect_hash: str = "") -> ToolResponse:
        try:
            p = resolve_in_root(self.ctx.project_root, path)
        except (PathEscapeError, DeniedPathError) as e:
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.DENIED, tool="godot_write_script", message=str(e)))
        # ★ 先快照后写入（顺序铁律：反了没有回头路）
        self.ctx.checkpoints.snapshot(p, reason=f"write_script {path}")
        resp = await self.ctx.store.write(path, content, expect_hash)
        if not resp.ok:
            return resp                          # CONFLICT：不自动三方合并，让模型重读重改

        self.ctx.checkpoints.snapshot(p, reason=f"write_script {path}")
        summary = resp.summary
        if self.ctx.auto_check:
            ok, note = await _auto_check(self.ctx, script=path)
            summary += "\n" + note
            if not ok:
                return ToolResponse(
                    ok=False, summary=summary,
                    error=ToolError(
                        kind=ErrorKind.VALIDATION, tool="godot_write_script",
                        message="已写入但 L1 语法校验未通过（错误行号见上）",
                        hint="按行号修复后重写；常见：缩进用 Tab、未定义变量、"
                             "信号签名不匹配"),
                    artifacts=resp.artifacts)
        summary += "\n下一步: 若改了场景引用的 API，可用 godot_edit_scene 连线或调属性"
        return ToolResponse(ok=True, summary=summary, artifacts=resp.artifacts)


@_godot_tool("godot_list_symbols", readonly=True)
class GodotListSymbolsTool(BaseTool):
    """扫描全部 .gd 生成项目符号表（类/信号/函数 + 行号）——跨文件找 API 用。"""
    class Params(BaseModel):
        path: str = Field(default=".",
                          description="扫描根目录（默认整个项目）")
        name: str = Field(default="",
                          description="可选，只保留名字包含该子串的符号")

    def __init__(self, ctx: GodotContext):
        self.ctx = ctx

    async def run(self, path: str = ".", name: str = "") -> ToolResponse:
        try:
            base = resolve_in_root(self.ctx.project_root, path)
        except (PathEscapeError, DeniedPathError) as e:
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.DENIED, tool="godot_list_symbols", message=str(e)))
        files = ([base] if base.is_file()
                 else sorted(p for p in base.rglob("*.gd")
                             if not any(x in DENY_PARTS for x in p.parts)))
        rows: list[str] = []
        for f in files:
            rel = str(f.relative_to(self.ctx.project_root)).replace("\\", "/")
            syms = gd_symbols(f.read_text(encoding="utf-8", errors="replace"))
            hits = [f"L{ln} {kind} {nm}" for ln, kind, nm in syms
                    if not name or name in nm]
            if hits:
                rows.append(f"{rel}: " + ", ".join(hits[:20]))
        if not rows:
            return ToolResponse(ok=True,
                                summary=f"未找到匹配的符号（过滤词 {name!r}）"
                                if name else "项目里没有 .gd 脚本")
        return ToolResponse(ok=True, summary="\n".join(rows),
                            data={"files": len(rows)})
