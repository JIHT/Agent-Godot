"""tests/test_tools/test_registry.py —— 注册表与 Schema 清洗单测（M04 §5）。"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from agent_godot.tools import BaseTool, ToolRegistry, ToolResponse, to_fc_schema
from agent_godot.tools.builtin import build_default_registry


def test_schema_cleaned():
    """嵌套模型的 schema 清洗：无 $ref/$defs/title。"""

    class Inner(BaseModel):
        x: int = Field(description="内层参数")

    class NestedParams(BaseModel):
        name: str
        inner: Inner

    spec = to_fc_schema(NestedParams)
    dumped = json.dumps(spec)
    assert "$ref" not in dumped and "$defs" not in dumped
    assert "title" not in spec
    assert spec["properties"]["inner"]["properties"]["x"]["type"] == "integer"


def test_registry_filter_for_subagent():
    """filter(readonly=True) 的视图里没有 write_file——ask 模式的物理边界。"""
    reg = build_default_registry(Path("."))
    view = reg.filter(readonly=True)
    assert "write_file" not in view.names()
    assert "read_file" in view.names()
    assert "list_files" in view.names()


def test_registry_namespaced():
    """命名空间视图：工具名加前缀（M05 MCP 桥接防重名）。"""
    reg = build_default_registry(Path("."))
    view = reg.namespaced("mcp__godot")
    assert "mcp__godot__read_file" in view.names()
    assert view.get("mcp__godot__read_file") is not None


def test_builtin_six_tools_registered():
    """六件套全部就位：read/write/list/search/diff/todo。"""
    reg = build_default_registry(Path("."))
    names = set(reg.names())
    assert {"read_file", "write_file", "list_files",
            "search_files", "diff", "todo_write"} <= names


async def test_execute_validates_params(tmp_path: Path):
    """execute 入口的参数校验：非法 JSON / 类型错误 → VALIDATION 响应（不抛）。"""
    reg = build_default_registry(tmp_path)
    tool = reg.get("read_file")

    r = await tool.execute("not a json")
    assert not r.ok and "VALIDATION" not in r.render()  # kind 渲染为小写
    assert "json" in r.render().lower()

    r2 = await tool.execute('{"path": 123}')      # path 必须是字符串
    assert not r2.ok

    r3 = await tool.execute('{"path": "x.txt"}')  # 合法参数 → 正常执行
    assert r3.ok is False and r3.error.kind.value == "not_found"  # 文件不存在（业务失败≠校验失败）
