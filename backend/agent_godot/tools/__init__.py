"""tools：工具系统（M04）—— 注册表（pydantic Schema 自动生成）、沙箱、乐观锁。

三大件：
- registry：洞洞板（@register_tool 装饰器注册，BaseTool + Params = 单一事实源）
- sandbox：三道安全闸（路径防护 / 超时 / 输出截断）
- file_lock：乐观锁文件读写（content hash 版本号，冲突不自动合并）

builtin/：六件套（read/write/list/search/diff/todo）。
"""
from .file_lock import OptimisticFileStore, sha16
from .registry import (BaseTool, ToolMeta, ToolRegistry, register_tool)
from .response import Artifact, ErrorKind, ToolError, ToolResponse
from .sandbox import (DENY_PARTS, DeniedPathError, PathEscapeError,
                      resolve_in_root, truncate)
from .schema import to_fc_schema

__all__ = [
    "OptimisticFileStore", "sha16",
    "BaseTool", "ToolMeta", "ToolRegistry", "register_tool",
    "Artifact", "ErrorKind", "ToolError", "ToolResponse",
    "DENY_PARTS", "DeniedPathError", "PathEscapeError", "resolve_in_root",
    "truncate", "to_fc_schema",
]
