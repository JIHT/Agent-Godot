"""tests/test_mcp/test_session.py —— 会话层测试（M05 §5）。

两组：
① ScriptedTransport 注入——测乱序配对 / 孤儿响应（纯内存，无进程）
② 真子进程 fake_server——测完整握手 / 工具发现调用 / 死亡清算
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from agent_godot.mcp.client import (McpSession, McpTransportError, Transport)
from agent_godot.mcp.client.transport import StdioTransport

FAKE_SERVER = Path(__file__).resolve().parents[3] / "lab" / "m05" / "fake_server.py"


# ---------- ① 脚本化传输（测时序问题）----------

class ScriptedTransport(Transport):
    """内存传输：send 只记录，测试手动喂响应（可任意乱序）。"""

    def __init__(self):
        self.sent: list[str] = []

    async def start(self) -> None: ...

    async def send(self, line: str) -> None:
        self.sent.append(line)

    async def close(self) -> None: ...


async def test_out_of_order_responses():
    """响应乱序到达：id=2 先回、id=1 后回 → 两个 await 各自拿到正确结果。"""
    t = ScriptedTransport()
    s = McpSession(t, timeout=5)
    f1 = asyncio.create_task(s.request("method_a", {}))
    f2 = asyncio.create_task(s.request("method_b", {}))
    await asyncio.sleep(0.05)                    # 等两个请求都发出（id=1,2）
    await t._on_message('{"jsonrpc":"2.0","id":2,"result":"B"}')   # ★ 故意先回 2
    await t._on_message('{"jsonrpc":"2.0","id":1,"result":"A"}')
    assert await f1 == "A"
    assert await f2 == "B"


async def test_orphan_response_ignored():
    """孤儿响应（无对应 pending）：静默忽略不炸。"""
    t = ScriptedTransport()
    s = McpSession(t, timeout=5)
    await t._on_message('{"jsonrpc":"2.0","id":99,"result":"late"}')


async def test_tools_list_changed_invalidates_cache():
    """list_changed 通知 → 工具缓存失效。"""
    t = ScriptedTransport()
    s = McpSession(t, timeout=5)
    s._tools_cache = [{"name": "old"}]

    async def fake_request(method, params):
        return {"tools": [{"name": "new"}]}
    s.request = fake_request                     # type: ignore[assignment]
    await t._on_message('{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}')
    assert [t_["name"] for t_ in await s.list_tools()] == ["new"]


# ---------- ② 真子进程集成（fake_server）----------

async def _make_session() -> McpSession:
    t = StdioTransport(sys.executable, [str(FAKE_SERVER)])
    s = McpSession(t, timeout=10)
    await t.start()
    return s


async def test_initialize_handshake_and_list_tools():
    """完整握手 → 工具发现：echo/add 两个工具可见。"""
    s = await _make_session()
    try:
        caps = await s.initialize()
        assert "tools" in caps
        assert s.server_info["name"] == "fake"
        names = [t["name"] for t in await s.list_tools()]
        assert "echo" in names and "add" in names
    finally:
        await s.close()


async def test_call_tool_roundtrip():
    """echo 回显 / add 加法：参数经 JSON-RPC 往返正确。"""
    s = await _make_session()
    try:
        await s.initialize()
        r = await s.call_tool("echo", {"text": "hello-mcp"})
        assert r.ok and "hello-mcp" in r.summary
        r2 = await s.call_tool("add", {"a": 2, "b": 3})
        assert r2.ok and "5" in r2.summary
        # 工具级错误（isError）映射 ok=False
        r3 = await s.call_tool("nope", {})
        assert not r3.ok
    finally:
        await s.close()


async def test_pending_cleared_on_server_death():
    """杀掉服务器进程 → 挂起/后续调用秒级失败（而非等到超时）。"""
    s = await _make_session()
    await s.initialize()
    assert s.transport.proc is not None
    s.transport.proc.kill()                      # ★ 模拟服务器崩溃
    await asyncio.sleep(0.3)                     # 等 stdout 泵读到 EOF 清算
    r = await s.call_tool("echo", {"text": "x"})
    assert not r.ok and "离线" in (r.error.message if r.error else "")
    with __import__("pytest").raises(McpTransportError):
        await s.request("tools/list", {})
