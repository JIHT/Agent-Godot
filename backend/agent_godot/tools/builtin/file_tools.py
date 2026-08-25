"""tools/builtin/file_tools.py —— 文件四件套（M04 §1.5 / §4 步骤 6）

ReadFile / WriteFile（乐观锁）/ ListFiles / SearchFiles。
read 的 summary 尾部带 hash——模型下一轮 write 时作为 expect_hash 传回，
这是"交接班制度"闭环的关键。
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..file_lock import OptimisticFileStore
from ..registry import BaseTool, register_tool
from ..response import ErrorKind, ToolError, ToolResponse
from ..sandbox import DeniedPathError, PathEscapeError, resolve_in_root, truncate

# 搜索工具只碰文本类文件（二进制读进来也是乱码还浪费预算）
TEXT_SUFFIXES = {".py", ".md", ".txt", ".gd", ".tscn", ".tres", ".json",
                 ".yaml", ".yml", ".toml", ".cfg", ".ini", ".csv", ".js", ".ts"}


def _denied(tool: str, e: Exception) -> ToolResponse:
    """沙箱拦截 → 干净的 DENIED 响应（被拦的攻击也是一次'正常失败'）。"""
    return ToolResponse(ok=False, error=ToolError(
        kind=ErrorKind.DENIED, tool=tool, message=str(e),
        hint="路径在禁止访问范围内，请改用项目内的合法路径"))


@register_tool(name="read_file", readonly=True, risk="low", tags={"fs"})
class ReadFileTool(BaseTool):
    """读取项目内文本文件。修改任何文件前必须先调用它获取当前内容与版本 hash。"""
    class Params(BaseModel):
        path: str = Field(description="项目相对路径，如 'lab/m01/sampler.py'")
        max_bytes: int = Field(default=100_000, ge=100, le=500_000,
                               description="最多读取字节数（默认 10 万）")

    def __init__(self, store: OptimisticFileStore):
        self.store = store

    async def run(self, path: str, max_bytes: int = 100_000) -> ToolResponse:
        try:
            content, h = await self.store.read(path)
        except (PathEscapeError, DeniedPathError) as e:
            return _denied("read_file", e)
        if h == "":
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.NOT_FOUND, tool="read_file",
                message=f"文件不存在: {path}",
                hint="用 list_files 确认正确路径"))
        text = content[:max_bytes]
        note = f"\n\n--- 当前版本 hash: {h}（write_file 时作为 expect_hash 传入）"
        if len(content) > max_bytes:
            note = f"\n\n[已截断，全文 {len(content)} 字符，仅显示前 {max_bytes}]" + note
        return ToolResponse(ok=True, summary=truncate(text) + note,
                            data={"hash": h, "length": len(content)})


@register_tool(name="write_file", readonly=False, risk="medium", tags={"fs"})
class WriteFileTool(BaseTool):
    """写入文本文件（乐观锁）：必须传 read_file 返回的 hash，新建文件传空字符串。"""
    class Params(BaseModel):
        path: str = Field(description="目标文件相对路径")
        content: str = Field(description="完整的新文件内容（整体覆盖）")
        expect_hash: str = Field(default="",
                                 description="read_file 返回的当前版本 hash；新文件传空字符串")

    def __init__(self, store: OptimisticFileStore):
        self.store = store

    async def run(self, path: str, content: str, expect_hash: str) -> ToolResponse:
        try:
            return await self.store.write(path, content, expect_hash)
        except (PathEscapeError, DeniedPathError) as e:
            return _denied("write_file", e)


@register_tool(name="list_files", readonly=True, risk="low", tags={"fs"})
class ListFilesTool(BaseTool):
    """列出目录下的文件与子目录（含一层展开，过滤敏感目录）。"""
    class Params(BaseModel):
        path: str = Field(default=".", description="目录相对路径，默认项目根")
        pattern: str = Field(default="", description="可选 glob 过滤，如 '*.py'")

    def __init__(self, store: OptimisticFileStore):
        self.store = store

    async def run(self, path: str = ".", pattern: str = "") -> ToolResponse:
        try:
            base = resolve_in_root(self.store.root, path)
        except (PathEscapeError, DeniedPathError) as e:
            return _denied("list_files", e)
        if not base.exists():
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.NOT_FOUND, tool="list_files",
                message=f"路径不存在: {path}", hint="确认目录名"))
        if base.is_file():
            return ToolResponse(ok=True, summary=f"{path}（这是一个文件）")

        from ..sandbox import DENY_PARTS
        if pattern:
            entries = sorted(str(p.relative_to(self.store.root))
                             for p in base.rglob(pattern)
                             if not any(x in DENY_PARTS for x in p.parts))
        else:
            entries = sorted(
                (p.name + ("/" if p.is_dir() else ""))
                for p in base.iterdir() if p.name not in DENY_PARTS)
        if not entries:
            return ToolResponse(ok=True, summary=f"{path} 是空目录")
        shown = entries[:200]
        more = f"\n...（共 {len(entries)} 项，仅显示前 200）" if len(entries) > 200 else ""
        return ToolResponse(ok=True, summary="\n".join(shown) + more,
                            data={"count": len(entries)})


@register_tool(name="search_files", readonly=True, risk="low", tags={"fs"})
class SearchFilesTool(BaseTool):
    """在项目文本文件中搜索包含指定子串的行（简易 grep，返回 文件:行号:行内容）。"""
    class Params(BaseModel):
        pattern: str = Field(description="要搜索的文本子串")
        path: str = Field(default=".", description="搜索的根目录")
        max_results: int = Field(default=50, ge=1, le=200,
                                 description="最多返回命中行数")

    def __init__(self, store: OptimisticFileStore):
        self.store = store

    async def run(self, pattern: str, path: str = ".",
                  max_results: int = 50) -> ToolResponse:
        try:
            base = resolve_in_root(self.store.root, path)
        except (PathEscapeError, DeniedPathError) as e:
            return _denied("search_files", e)
        if not base.exists():
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.NOT_FOUND, tool="search_files",
                message=f"路径不存在: {path}"))

        from ..sandbox import DENY_PARTS
        hits: list[str] = []
        files = ([base] if base.is_file()
                 else [p for p in base.rglob("*")
                       if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES
                       and not any(x in DENY_PARTS for x in p.parts)])
        for f in files:
            try:
                if f.stat().st_size > 1_000_000:   # 跳过超大文件
                    continue
                for i, line in enumerate(
                        f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if pattern in line:
                        rel = f.relative_to(self.store.root)
                        hits.append(f"{rel}:{i}: {line.strip()[:120]}")
                        if len(hits) >= max_results:
                            break
            except OSError:
                continue
            if len(hits) >= max_results:
                break
        if not hits:
            return ToolResponse(ok=True,
                                summary=f"未找到包含 {pattern!r} 的行",
                                data={"hits": 0})
        return ToolResponse(ok=True, summary="\n".join(hits),
                            data={"hits": len(hits)})
