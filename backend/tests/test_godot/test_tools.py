"""tests/test_godot/test_tools.py —— 12 个领域 FC 工具（M06 §4 步骤 5 / §5）。

不起真 Godot（find_godot 被打桩 → headless 校验自动跳过并注明）；
真机全链路（read_scene→edit→check）留给 MI-1 验收 Demo。
"""
import json
import shutil
from pathlib import Path

import pytest

from agent_godot.tools import ToolRegistry
from agent_godot.tools.godot import build_godot_context, register_godot_tools
from agent_godot.tools.godot.check_tools import GodotCheckTool
from agent_godot.tools.godot.scene_tools import (GodotCreateSceneTool,
                                                 GodotEditSceneTool,
                                                 GodotReadSceneTool)
from agent_godot.tools.godot.scenes import parse_tscn
from agent_godot.tools.godot.script_tools import (GodotListSymbolsTool,
                                                  GodotReadScriptTool,
                                                  GodotWriteScriptTool)

SAMPLE = Path(__file__).parents[3] / "lab" / "m06" / "sample"


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agent_godot.tools.godot.headless.find_godot",
                        lambda: None)     # 强制无 Godot：校验走"跳过"分支
    return build_godot_context(tmp_path)


@pytest.fixture
def proj(tmp_path: Path, monkeypatch):
    shutil.copytree(SAMPLE, tmp_path, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".godot", "__pycache__"))
    monkeypatch.setattr("agent_godot.tools.godot.headless.find_godot",
                        lambda: None)
    return build_godot_context(tmp_path)


# ---------- 注册与元数据 ----------

def test_register_godot_tools(proj, tmp_path):
    reg = ToolRegistry()
    register_godot_tools(reg, tmp_path)
    names = reg.names()
    assert len(names) == 12
    assert {"godot_open_project", "godot_read_scene", "godot_edit_scene",
            "godot_write_script", "godot_check", "godot_run_scene"} <= set(names)
    # Dispatcher 分流元数据：读并发 / 写串行 / headless 120s 档
    assert reg.get("godot_read_scene").meta.readonly
    assert not reg.get("godot_edit_scene").meta.readonly
    assert "headless" in reg.get("godot_check").meta.tags
    # FC 声明可生成（喂模型）
    specs = reg.tool_specs()
    assert all(s.parameters for s in specs)
    assert all(s.description for s in specs)


# ---------- 场景域 ----------

async def test_read_scene_renders_tree(proj):
    resp = await GodotReadSceneTool(proj).execute('{"scene": "main.tscn"}')
    assert resp.ok
    assert "[instance: res://player.tscn]" in resp.summary
    assert "信号连线" in resp.summary and "health_changed" in resp.summary
    assert "可编辑节点" in resp.summary            # "下一步线索"
    assert resp.data["tree"]["children"][0]["name"] == "Main"


async def test_read_scene_missing(proj):
    resp = await GodotReadSceneTool(proj).execute('{"scene": "nope.tscn"}')
    assert not resp.ok
    assert resp.error.kind.value == "not_found"


async def test_edit_scene_full_chain(proj):
    """验收链路核心：add_node(带脚本) → set_prop → connect_signal → 落盘可再解析。"""
    ops = [
        {"op": "add_node", "parent": ".", "name": "Trap", "type": "Area2D",
         "script": "res://trap.gd"},
        {"op": "set_prop", "path": "Trap", "key": "position",
         "value": "Vector2(10, 10)"},
        {"op": "connect_signal", "signal": "body_entered", "from": "Trap",
         "to": ".", "method": "_on_trap"},
    ]
    resp = await GodotEditSceneTool(proj).execute(
        json.dumps({"scene": "main.tscn", "ops": ops}))
    assert resp.ok
    assert "校验: 已跳过" in resp.summary           # 无 Godot → 明确跳过

    text = (proj.project_root / "main.tscn").read_text(encoding="utf-8")
    sf = parse_tscn(text)
    trap = sf.find("Trap")
    assert trap.type == "Area2D"
    assert proj.store and sf.resources[trap.script]["attrs"]["path"] == "res://trap.gd"
    assert trap.props["position"] == "Vector2(10, 10)"
    assert any(c["from"] == "Trap" for c in sf.connections)

    # 写前检查点已生成，rewind 可完整恢复
    infos = proj.checkpoints.list()
    assert infos and infos[-1].snapshots >= 1
    original = (SAMPLE / "main.tscn").read_text(encoding="utf-8")
    proj.checkpoints.rollback()
    assert (proj.project_root / "main.tscn").read_text(encoding="utf-8") == original


async def test_edit_scene_bad_op_leaves_file_untouched(proj):
    target = proj.project_root / "main.tscn"
    before = target.read_text(encoding="utf-8")
    ops = [{"op": "add_node", "parent": "Ghost", "name": "X", "type": "Node"}]
    resp = await GodotEditSceneTool(proj).execute(
        json.dumps({"scene": "main.tscn", "ops": ops}))
    assert not resp.ok
    assert resp.error.kind.value == "validation"
    assert target.read_text(encoding="utf-8") == before   # 坏数据不落盘


async def test_create_scene(proj):
    resp = await GodotCreateSceneTool(proj).execute(json.dumps(
        {"path": "scenes/trap.tscn", "root_name": "Trap",
         "root_type": "Area2D", "script": "res://trap.gd"}))
    assert resp.ok
    p = proj.project_root / "scenes" / "trap.tscn"
    text = p.read_text(encoding="utf-8")
    sf = parse_tscn(text)
    assert sf.nodes[0].name == "Trap" and sf.nodes[0].type == "Area2D"
    assert "uid://" in text
    assert SceneFile_roundtrip(text)


def SceneFile_roundtrip(text: str) -> bool:
    from agent_godot.tools.godot.scenes import SceneFile
    return SceneFile.serialize(parse_tscn(text)) == text


# ---------- 脚本域 ----------

async def test_read_script_with_outline_and_hash(proj):
    resp = await GodotReadScriptTool(proj).execute('{"path": "player.gd"}')
    assert resp.ok
    assert "func take_damage" in resp.summary
    assert "signal health_changed" in resp.summary
    assert resp.data["hash"]


async def test_write_script_optimistic_lock(proj):
    writer = GodotWriteScriptTool(proj)
    reader = GodotReadScriptTool(proj)

    # 读 → 拿 hash
    r = await reader.execute('{"path": "player.gd"}')
    good_hash = r.data["hash"]

    # 错误 hash → CONFLICT，内容不落盘
    bad = await writer.execute(json.dumps(
        {"path": "player.gd", "content": "hacked",
         "expect_hash": "deadbeefdeadbeef"}))
    assert not bad.ok and bad.error.kind.value == "conflict"

    # 正确 hash → 写入成功
    ok = await writer.execute(json.dumps(
        {"path": "player.gd", "content": "extends Node2D\n",
         "expect_hash": good_hash}))
    assert ok.ok
    assert (proj.project_root / "player.gd").read_text(encoding="utf-8") == \
        "extends Node2D\n"


async def test_list_symbols(proj):
    resp = await GodotListSymbolsTool(proj).execute('{"path": "."}')
    assert resp.ok
    assert "take_damage" in resp.summary
    assert "health_changed" in resp.summary

    resp2 = await GodotListSymbolsTool(proj).execute(
        '{"path": ".", "name": "take_damage"}')
    assert "take_damage" in resp2.summary


# ---------- 校验域（无 Godot 的降级路径） ----------

async def test_check_without_godot(proj):
    resp = await GodotCheckTool(proj).execute("{}")
    assert not resp.ok
    assert "GODOT_BIN" in (resp.error.hint or "")


async def test_open_project_and_overview(proj):
    from agent_godot.tools.godot.scene_tools import (GodotOpenProjectTool,
                                                     GodotProjectOverviewTool)
    resp = await GodotOpenProjectTool(proj).execute("{}")
    assert resp.ok
    assert "AgentGodotSample" in resp.summary
    assert "3 个场景" in resp.summary and "3 个脚本" in resp.summary

    resp2 = await GodotProjectOverviewTool(proj).execute("{}")
    assert "res://main.tscn" in resp2.summary
    assert "不可用" in resp2.summary                  # headless 状态如实上报
