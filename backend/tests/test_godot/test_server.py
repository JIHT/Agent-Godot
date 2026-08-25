"""tests/test_godot/test_server.py —— 自研 Godot MCP 服务器（M06 §4 步骤 7）。

直接驱动 handle()（一行 JSON-RPC 进、一行响应出）——与 M05 客户端同一套协议。
"""
import json
import shutil
from pathlib import Path

import pytest

from agent_godot.mcp.servers.godot.server import GodotMcpServer

SAMPLE = Path(__file__).parents[3] / "lab" / "m06" / "sample"


@pytest.fixture
def server(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agent_godot.tools.godot.headless.find_godot",
                        lambda: None)
    shutil.copytree(SAMPLE, tmp_path, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".godot", "__pycache__"))
    return GodotMcpServer(tmp_path)


def _req(id_, method, params=None):
    return json.dumps({"jsonrpc": "2.0", "id": id_, "method": method,
                       **({"params": params} if params is not None else {})})


async def test_initialize(server):
    resp = json.loads(await server.handle(_req(1, "initialize", {
        "protocolVersion": "2025-03-26"})))
    assert resp["id"] == 1
    assert "tools" in resp["result"]["capabilities"]   # 声明 tools 能力（空对象即存在）
    assert resp["result"]["serverInfo"]["name"] == "agent-godot-godot"


async def test_notification_gets_no_response(server):
    assert await server.handle(json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/initialized"})) is None


async def test_ping(server):
    resp = json.loads(await server.handle(_req(7, "ping")))
    assert resp["result"] == {}


async def test_tools_list_exposes_12_tools(server):
    resp = json.loads(await server.handle(_req(2, "tools/list")))
    tools = resp["result"]["tools"]
    assert len(tools) == 12
    assert any(t["name"] == "godot_read_scene" for t in tools)
    assert all("inputSchema" in t and "description" in t for t in tools)


async def test_tools_call_read_scene(server):
    resp = json.loads(await server.handle(_req(3, "tools/call", {
        "name": "godot_read_scene", "arguments": {"scene": "main.tscn"}})))
    result = resp["result"]
    assert not result["isError"]
    assert "Player" in result["content"][0]["text"]


async def test_tools_call_unknown_tool_is_result_error(server):
    """工具级错误走 isError 结果（不是 JSON-RPC error）——错误也是数据。"""
    resp = json.loads(await server.handle(_req(4, "tools/call", {
        "name": "no_such_tool", "arguments": {}})))
    assert "error" not in resp
    assert resp["result"]["isError"] is True
    assert "no_such_tool" in resp["result"]["content"][0]["text"]


async def test_unknown_method(server):
    resp = json.loads(await server.handle(_req(5, "foo/bar")))
    assert resp["error"]["code"] == -32601


async def test_parse_error(server):
    resp = json.loads(await server.handle("this is not json"))
    assert resp["error"]["code"] == -32700


async def test_response_is_single_line_json(server):
    """stdio 分帧铁律：响应必须是单行 JSON（禁 indent）。"""
    resp = await server.handle(_req(6, "tools/list"))
    assert "\n" not in resp
    json.loads(resp)                                 # 单行且合法
