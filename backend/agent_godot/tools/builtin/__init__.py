"""tools/builtin：内置 FC 工具六件套（M04 §4 步骤 6/7）。

build_default_registry 是组装工厂：创建 OptimisticFileStore（共享乐观锁
状态）→ 实例化六件套 → 注册。root 默认当前工作目录（CLI 从项目根运行）。
"""
from __future__ import annotations

from pathlib import Path

from ..file_lock import OptimisticFileStore
from ..registry import ToolRegistry
from .diff_tool import DiffTool
from .file_tools import (ListFilesTool, ReadFileTool, SearchFilesTool,
                         WriteFileTool)
from .todowrite_tool import TodoWriteTool

__all__ = ["build_default_registry", "ReadFileTool", "WriteFileTool",
           "ListFilesTool", "SearchFilesTool", "DiffTool", "TodoWriteTool"]


def build_default_registry(root: Path | None = None) -> ToolRegistry:
    """组装内置工具集（含文件四件套 + diff + todo）。

    root：项目根（沙箱白名单边界 + 乐观锁作用域），默认 cwd。
    """
    store = OptimisticFileStore(root or Path.cwd())
    reg = ToolRegistry()
    reg.register(ReadFileTool(store))
    reg.register(WriteFileTool(store))
    reg.register(ListFilesTool(store))
    reg.register(SearchFilesTool(store))
    reg.register(DiffTool())
    reg.register(TodoWriteTool())
    return reg
