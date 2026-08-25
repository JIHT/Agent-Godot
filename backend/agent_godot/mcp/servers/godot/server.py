"""mcp/servers/godot/server.py —— 自研 Godot MCP 服务器（M06 §4 步骤 7）

M05 客户端的镜像：协议同一套（JSON-RPC 2.0 / stdio 单行分帧），
复用 client/jsonrpc.py 的编解码——initialize 回能力 {tools} → tools/list
返回工具清单（复用 register_godot_tools 的注册表）→ tools/call 分发执行
→ stdout 单行写回。

与 M05 客户端会合后，`mcp__godot__*` 工具即可被桥接进 Agent（同一实现的
第二个出口：CLI 本地注册 + MCP 远程，零重复代码）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from agent_godot.tools import ErrorKind, ToolError, ToolResponse
from agent_godot.tools.godot import register_godot_tools

from ...client.jsonrpc import RPCRequest, decode  # 镜像：复用同一套编解码

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "agent-godot-godot", "version": "0.1.0"}


class _MethodNotFound(Exception):
    """未知 JSON-RPC 方法（→ -32601）。"""


class GodotMcpServer:
    """stdio MCP 服务器：handle() 收一行 JSON-RPC，返回一行响应（通知返回 None）。"""

    def __init__(self, project_root: Path, godot_bin: str | None = None):
        from agent_godot.tools import ToolRegistry
        self.registry = ToolRegistry()
        self.ctx = register_godot_tools(self.registry, project_root, godot_bin)

    async def handle(self, raw: str) -> str | None:
        try:
            msg = decode(raw)
        except ValueError:
            return _respond_error(None, -32700, "Parse error")
        if not isinstance(msg, RPCRequest):
            return None                      # 通知（notifications/initialized）无响应
        try:
            result = await self._dispatch(msg.method, msg.params or {})
        except _MethodNotFound as e:
            return _respond_error(msg.id, -32601, str(e))
        except Exception as e:               # noqa: BLE001 —— 协议层兜底
            return _respond_error(msg.id, -32603,
                                  f"Internal error: {type(e).__name__}: {e}")
        return _respond_result(msg.id, result)

    async def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return {"protocolVersion": params.get("protocolVersion",
                                                  PROTOCOL_VERSION),
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [
                {"name": s.name, "description": s.description,
                 "inputSchema": s.parameters}
                for s in self.registry.tool_specs()]}
        if method == "tools/call":
            return await self._call_tool(params)
        raise _MethodNotFound(f"未知方法: {method}")

    async def _call_tool(self, params: dict) -> dict:
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if not self.registry.has(name):
            resp = ToolResponse(ok=False, error=ToolError(
                ErrorKind.NOT_FOUND, name,
                f"未注册的工具: {name}",
                hint=f"从 tools/list 选择，可用: {self.registry.names()}"))
        else:
            resp = await self.registry.get(name).execute(
                json.dumps(arguments, ensure_ascii=False))
        # MCP 工具级错误走 isError 结果（不是 JSON-RPC error）——错误也是数据
        return {"content": [{"type": "text", "text": resp.render_for_model()}],
                "isError": not resp.ok}


def _respond_result(id_, result) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "result": result},
                      ensure_ascii=False)


def _respond_error(id_, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_,
                       "error": {"code": code, "message": message}},
                      ensure_ascii=False)


async def _stdio_loop(server: GodotMcpServer) -> None:
    """stdin 逐行读（executor 防阻塞事件循环）→ handle → stdout 单行写回。

    首行防御性剥离 BOM（Windows PowerShell 管道会给流首加 EF BB BF）。
    """
    loop = asyncio.get_running_loop()
    while True:
        raw = await loop.run_in_executor(None, sys.stdin.readline)
        if not raw:                          # EOF：客户端进程退出
            break
        raw = raw.strip().lstrip("\ufeff").strip()
        if not raw:
            continue
        resp = await server.handle(raw)
        if resp is not None:
            print(resp, flush=True)          # ★ 单行 JSON，禁 indent


def serve(project_root: Path | None = None, godot_bin: str | None = None) -> None:
    """stdio 起服（python -m agent_godot.mcp.servers.godot）。"""
    import os
    sys.stdout.reconfigure(encoding="utf-8")     # Windows GBK 终端防线
    sys.stdin.reconfigure(encoding="utf-8")
    root = Path(project_root or os.environ.get("AGENT_GODOT_ROOT", ".")).resolve()
    bin_ = godot_bin or os.environ.get("GODOT_BIN")
    server = GodotMcpServer(root, bin_)
    asyncio.run(_stdio_loop(server))
