"""mcp/client/session.py —— 会话生命周期（M05 §1.4 / §3 难点 / §4 步骤 3）

入职握手仪式：initialize（递简历）→ 服务器回能力（部门权限）→
notifications/initialized（确认开工）→ tools/list（领工具）→ tools/call（干活）。

§3 难点——pending 配对的四个时序问题全在这台机器上：
- 乱序：响应按 id 查 pending 字典，与到达顺序无关
- 超时：wait_for 超时后 future 出列，上游拿到 McpTransportError
- 孤儿响应：超时后迟到的响应 → 记 warning 丢弃（绝不能配错）
- 服务器死亡：stdout EOF → on_disconnect → 全部 pending 立即错误兑现
"""
from __future__ import annotations

import asyncio
import logging

from agent_godot.tools import ErrorKind, ToolError, ToolResponse

from .jsonrpc import (McpRemoteError, RPCNotification, RPCRequest,
                      RPCResponse, decode, encode)
from .transport import McpTransportError, Transport

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "agent-godot", "version": "0.1.0"}


class McpSession:
    """一个 MCP 服务器的会话（握手状态机 + 请求配对 + 工具缓存）。"""

    def __init__(self, transport: Transport, timeout: float = 30.0):
        self.transport = transport
        self.timeout = timeout
        self._state = "new"                     # new → ready → dead
        self._dead = False
        self._pending: dict[str | int, asyncio.Future] = {}
        self._next_id = 0
        self.server_caps: dict = {}
        self.server_info: dict = {}
        self._tools_cache: list[dict] | None = None

        transport.on_message(self._handle_raw)
        transport.on_disconnect(self._handle_disconnect)

    # ---------- 握手 ----------

    async def initialize(self) -> dict:
        """三步握手：initialize → 存服务器能力 → initialized 通知 → ready。

        顺序不可换：未 initialize 就调 tools/list 会被服务器按协议拒绝。
        """
        resp = await self.request("initialize", params={
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},                 # 我方能力（roots 等按需声明）
            "clientInfo": CLIENT_INFO})
        self.server_caps = resp.get("capabilities", {})
        self.server_info = resp.get("serverInfo", {})
        await self.notify("notifications/initialized")   # ★ 通知无响应，不能等
        self._state = "ready"
        return self.server_caps

    # ---------- 请求配对（§3 难点核心）----------

    async def request(self, method: str, params: dict) -> object:
        """发请求并等响应：id→future 注册 → send → wait_for → finally 清列。"""
        if self._dead:
            raise McpTransportError("服务器已离线")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._next_id += 1
        mid = self._next_id
        self._pending[mid] = fut
        try:
            await self.transport.send(encode(RPCRequest(method, params, mid)))
            return await asyncio.wait_for(fut, self.timeout)
        except asyncio.TimeoutError:
            raise McpTransportError(
                f"请求 {method} 超时（{self.timeout}s 无响应）") from None
        finally:
            self._pending.pop(mid, None)        # 超时/异常也要出列（防孤儿配错）

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self.transport.send(encode(RPCNotification(method, params)))

    async def _handle_raw(self, raw: str) -> None:
        """传输层回调：按消息形态分流。"""
        try:
            msg = decode(raw)
        except ValueError:
            logger.warning("MCP 服务器发来非 JSON 行: %.80s", raw)
            return

        if isinstance(msg, RPCResponse):
            fut = self._pending.get(msg.id)
            if fut is None or fut.done():
                logger.warning("孤儿响应 id=%s（超时后迟到，丢弃）", msg.id)
                return
            if msg.error:
                fut.set_exception(McpRemoteError(msg.error))
            else:
                fut.set_result(msg.result)
        elif isinstance(msg, RPCRequest):
            # 服务器反向请求（采样/elicitation）——M05 不实现，记日志即可
            logger.debug("服务器反向请求 %s（未实现，忽略）", msg.method)
        elif isinstance(msg, RPCNotification):
            if msg.method == "notifications/tools/list_changed":
                self._tools_cache = None         # ★ 服务器热加工具 → 缓存失效

    def _handle_disconnect(self) -> None:
        """服务器死亡：全部 pending 立即错误兑现（上游秒级失败而非挂到超时）。"""
        self._dead = True
        self._state = "dead"
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(McpTransportError("服务器进程退出"))
        self._pending.clear()

    # ---------- 工具发现与调用 ----------

    async def list_tools(self, force: bool = False) -> list[dict]:
        """工具发现：缓存优先；list_changed 通知或 force 时重拉。"""
        if self._tools_cache is None or force:
            result = await self.request("tools/list", {})
            self._tools_cache = (result or {}).get("tools", [])
        return self._tools_cache

    async def call_tool(self, name: str, arguments: dict) -> ToolResponse:
        """调用工具：content[] 的 text 块拼接为 summary；isError 映射 ok=False。

        服务器崩溃/超时 → 干净的 INTERNAL 响应（hint 提示换工具），
        绝不向上抛异常拖死 Loop——"错误也是数据"在 MCP 层的延续。
        """
        try:
            result = await self.request(
                "tools/call", {"name": name, "arguments": arguments})
        except McpRemoteError as e:
            return ToolResponse(ok=False, error=ToolError(
                ErrorKind.INTERNAL, name, f"服务器拒绝: {e}"))
        except McpTransportError as e:
            return ToolResponse(ok=False, error=ToolError(
                ErrorKind.INTERNAL, name, f"服务器离线/超时: {e}",
                hint="该 MCP 服务器不可用，请改用其他工具完成"))
        result = result or {}
        texts = [c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text"]
        summary = "\n".join(t for t in texts if t) or "(无文本输出)"
        return ToolResponse(ok=not result.get("isError", False), summary=summary)

    @property
    def state(self) -> str:
        return self._state

    async def close(self) -> None:
        await self.transport.close()
        self._handle_disconnect()
