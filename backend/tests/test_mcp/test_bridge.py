"""tests/test_mcp/test_bridge.py —— 桥接层测试（M05 §5）。"""
from __future__ import annotations

import sys
from pathlib import Path

from agent_godot.mcp.client import McpManager, McpToolBridge
from agent_godot.tools import ToolRegistry

FAKE_SERVER = Path(__file__).resolve().parents[3] / "lab" / "m05" / "fake_server.py"


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "mcp.yaml"
    py = str(sys.executable).replace("\\", "/")     # 正斜杠化：避开 yaml 双引号转义
    fake = str(FAKE_SERVER).replace("\\", "/")
    cfg.write_text(
        f'servers:\n'
        f'  fake:\n'
        f'    enabled: true\n'
        f'    transport: stdio\n'
        f'    command: "{py}"\n'
        f'    args: ["{fake}"]\n'
        f'  inactive-srv:\n'      # ★ 键名不能叫 off——YAML 1.1 会解析成布尔 False
        f'    enabled: false\n'
        f'    transport: stdio\n'
        f'    command: whatever\n',
        encoding="utf-8")
    return cfg


def test_looks_readonly_heuristic():
    """名称启发式：echo/add 只读；write/delete/run 类判写。"""
    from agent_godot.mcp.client.bridge import _looks_readonly
    assert _looks_readonly("echo") and _looks_readonly("godot_read_scene")
    assert not _looks_readonly("write_file")
    assert not _looks_readonly("godot_run_game")
    assert not _looks_readonly("godot_create_scene")


async def test_bridged_namespace_and_schema(tmp_path: Path):
    """fake 服务器经 McpManager 桥接：mcp__fake__* 工具入册且可真调用。"""
    reg = ToolRegistry()
    manager = McpManager(reg, config_path=_write_config(tmp_path))
    await manager.start_all()
    try:
        assert "mcp__fake__echo" in reg.names()
        tool = reg.get("mcp__fake__echo")
        assert tool.meta.name.startswith("mcp__fake__")

        # FC 声明：命名空间前缀 + inputSchema 已清洗（无 title）
        spec = tool.to_spec()
        assert spec.name == "mcp__fake__echo"
        assert "title" not in spec.parameters
        assert spec.parameters["properties"]["text"]["type"] == "string"

        # 真调用（经 dispatcher 的 execute 入口：JSON 字符串 → MCP 往返）
        r = await tool.execute('{"text": "bridged"}')
        assert r.ok and "bridged" in r.summary

        # 状态：fake 运行中 / off 已禁用
        status = manager.server_status()
        assert status["fake"] == "running"
        assert status["inactive-srv"] == "disabled"
    finally:
        await manager.stop_all()


async def test_bridge_wraps_tool_response_for_bad_json():
    """非法 JSON 参数 → 本地 VALIDATION 响应（不打服务器）。"""

    class _NeverCalledSession:
        async def call_tool(self, name, args):
            raise AssertionError("不应触达服务器")

    bridge = McpToolBridge("x", {"name": "echo", "description": "",
                                 "inputSchema": {"type": "object",
                                                 "properties": {}}},
                           _NeverCalledSession())    # type: ignore[arg-type]
    r = await bridge.execute("not json")
    assert not r.ok and "json" in r.render().lower()
