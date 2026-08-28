"""hooks/pre_tool：工具执行前的 hook（可否决 / 改参数）。

permission_hook：M09 权限门的正式化形态（priority=0，永远最先跑）。
"""
from .permission_hook import PermissionHook

__all__ = ["PermissionHook"]
