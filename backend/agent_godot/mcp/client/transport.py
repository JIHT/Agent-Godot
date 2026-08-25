"""mcp/client/transport.py —— 两种传输（M05 §1.3 / §4 步骤 2）

- StdioTransport：和坐在旁边的同事咬耳朵——subprocess 起服务器进程，
  stdin 写纸条 / stdout 收纸条 / stderr 收他的自言自语（日志）。本地 Godot 走它。
- HttpTransport：给外地供应商打电话——POST 到 Streamable HTTP endpoint，
  Mcp-Session-Id 头维持会话。联网服务走它。

两大死锁防线（§1.3 易错点）：
① stderr 必须单独起泵——服务器日志写满管道缓冲区（~64KB）会背压死锁
② 进程退出（stdout EOF）→ 触发 on_disconnect → session 清算全部 pending
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

# 消息回调与断连回调的类型
MessageCallback = Callable[[str], Awaitable[None]]
DisconnectCallback = Callable[[], None]


class McpTransportError(Exception):
    """传输层故障：进程退出/网络失败/超时。"""


class Transport(ABC):
    """传输抽象：session 只依赖这个接口，不感知 stdio 还是 HTTP。"""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def send(self, line: str) -> None: ...

    def on_message(self, cb: MessageCallback) -> None:
        self._on_message = cb

    def on_disconnect(self, cb: DisconnectCallback) -> None:
        self._on_disconnect = cb

    @abstractmethod
    async def close(self) -> None: ...


class StdioTransport(Transport):
    """子进程 stdio 传输：本地 MCP 服务器的标准形态。"""

    def __init__(self, command: str, args: list[str] | None = None,
                 env: dict | None = None, cwd: str | None = None):
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._on_message: MessageCallback | None = None
        self._on_disconnect: DisconnectCallback | None = None
        self._pumps: list[asyncio.Task] = []

    async def start(self) -> None:
        # Windows 深坑：npx 是 .cmd 批处理，create_subprocess_exec 不认——
        # shutil.which 会解析出 npx.cmd 的真实路径（跨平台通用）
        resolved = shutil.which(self.command) or self.command
        # env 必须合并父进程环境（PATH 等），完全替换会让服务器找不到 node/python
        merged = None
        if self.env:
            import os
            merged = {**os.environ, **self.env}
        self.proc = await asyncio.create_subprocess_exec(
            resolved, *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged, cwd=self.cwd)
        # ★ 双泵：stdout 按行分帧喂回调；stderr 只排空防死锁（不解析）
        self._pumps = [
            asyncio.create_task(self._pump_stdout()),
            asyncio.create_task(self._pump_stderr())]

    async def send(self, line: str) -> None:
        if self.proc is None or self.proc.returncode is not None:
            raise McpTransportError(
                f"服务器进程已退出 (code={self.proc.returncode if self.proc else '未启动'})")
        assert self.proc.stdin is not None
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def _pump_stdout(self) -> None:
        """按行分帧 = 消息边界；EOF = 进程退出 → 断连清算。"""
        assert self.proc and self.proc.stdout
        try:
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    break                        # ★ EOF：服务器进程退出
                msg = raw.decode("utf-8", "replace").strip()
                if not msg:
                    continue
                if self._on_message:
                    try:
                        await self._on_message(msg)
                    except Exception:            # noqa: BLE001 —— 泵不能被回调杀死
                        logger.exception("MCP 消息回调异常")
        finally:
            if self._on_disconnect:
                self._on_disconnect()

    async def _pump_stderr(self) -> None:
        """只排空防管道背压死锁，内容进日志。"""
        assert self.proc and self.proc.stderr
        while True:
            raw = await self.proc.stderr.readline()
            if not raw:
                break
            logger.debug("[mcp-stderr] %s", raw.decode("utf-8", "replace").strip())

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.proc.kill()
        for t in self._pumps:
            t.cancel()


class HttpTransport(Transport):
    """Streamable HTTP 传输（教学简化版）：每请求一个 POST，响应同步回来。

    完整规范允许服务器用 SSE 流回多个消息——本实现按"单 JSON 响应"处理
    （绝大多数服务器对请求-响应型 RPC 返回单 JSON），SSE 分片留 M06 升级。
    """

    def __init__(self, url: str, headers: dict | None = None,
                 timeout: float = 30.0):
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None      # Mcp-Session-Id 会话凭证
        self._on_message: MessageCallback | None = None
        self._on_disconnect: DisconnectCallback | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=self.timeout)

    async def send(self, line: str) -> None:
        if self._client is None:
            raise McpTransportError("HTTP 传输未启动")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            resp = await self._client.post(self.url, content=line, headers=headers)
        except httpx.HTTPError as e:
            if self._on_disconnect:
                self._on_disconnect()
            raise McpTransportError(f"HTTP 请求失败: {e}") from e
        if resp.status_code == 404:
            raise McpTransportError(f"MCP endpoint 不存在: {self.url}")
        if sid := resp.headers.get("mcp-session-id"):
            self._session_id = sid               # 首次响应携带会话凭证
        # 响应体：普通 JSON 单行，或 SSE 的 data: 行——统一逐行喂回调
        for out_line in resp.text.splitlines():
            payload = out_line.strip()
            if payload.startswith("data:"):
                payload = payload[5:].strip()
            if payload.startswith("{") and self._on_message:
                await self._on_message(payload)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
