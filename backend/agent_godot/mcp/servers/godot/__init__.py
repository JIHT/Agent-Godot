"""Godot MCP 服务器：把 M06 的 12 个领域工具包成 stdio MCP 服务器。

用法：python -m agent_godot.mcp.servers.godot --root <godot项目路径>
"""
from .server import GodotMcpServer, serve

__all__ = ["GodotMcpServer", "serve"]
