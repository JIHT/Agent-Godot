"""tools：工具系统（M04）—— 当前是最小种子，M03 的 Dispatcher 依赖它。

M03/M04 交替施工：这里先落下 registry + response + 两个内置只读工具，
够 M03 验收 demo（list_files → read_file → 总结）跑通。
M04 正式版在此之上加：参数 schema 校验、执行沙箱、乐观锁文件编辑、更多内置工具。
"""
from .registry import Tool, ToolRegistry
from .response import ToolResponse

__all__ = ["Tool", "ToolRegistry", "ToolResponse"]
