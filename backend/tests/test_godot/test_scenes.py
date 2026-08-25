"""tests/test_godot/test_scenes.py —— .tscn 解析/编辑/序列化（M06 §5）。

黄金测试：原样 parse → serialize 输出 == 输入（三个真实样本场景：
纯手搭树 / 实例化嵌套 / 信号连线复杂树）。
"""
from pathlib import Path

import pytest

from agent_godot.tools.godot.scenes import (SceneFile, SceneFormatError,
                                            SceneNode, parse_tscn)

SAMPLE = Path(__file__).parents[3] / "lab" / "m06" / "sample"

NESTED = """[gd_scene format=3]

[node name="World" type="Node2D"]

[node name="Body" type="CharacterBody2D" parent="."]

[node name="Sprite" type="Sprite2D" parent="Body"]

[node name="Shape" type="CollisionShape2D" parent="Body/Sprite"]
"""


def _read(name: str) -> str:
    return (SAMPLE / name).read_text(encoding="utf-8")


# ---------- 解析 ----------

def test_parse_plain_scene():
    sf = parse_tscn(_read("player.tscn"))
    assert len(sf.nodes) == 1
    root = sf.nodes[0]
    assert root.name == "Player" and root.type == "CharacterBody2D"
    assert root.parent == "."                        # 无 parent 属性 = 场景根
    assert root.script == "1_player"
    assert sf.resources["1_player"]["attrs"]["path"] == "res://player.gd"


def test_parse_instance_scene_opaque():
    sf = parse_tscn(_read("main.tscn"))
    tree = sf.tree()
    main_node = tree["children"][0]                  # 场景根 Main
    assert main_node["name"] == "Main"
    subs = main_node["children"]                     # 根的直接子节点
    assert [c["name"] for c in subs] == ["Player", "Enemy", "Camera"]
    assert subs[0]["opaque"] is True                 # 实例内部不展开
    assert subs[0]["instance"] == "res://player.tscn"
    assert subs[1]["opaque"] is True
    assert subs[2].get("opaque") is None             # 普通节点不标
    assert len(sf.connections) == 1
    assert sf.connections[0]["signal"] == "health_changed"
    assert sf.connections[0]["from"] == "Player"


def test_tree_nested_parent_paths():
    """parent 相对路径语义：parent="Body/Sprite" = 根下 Body 的 Sprite 里。"""
    sf = parse_tscn(NESTED)
    tree = sf.tree()
    world = tree["children"][0]
    assert world["name"] == "World"
    body = world["children"][0]
    assert body["name"] == "Body"
    sprite = body["children"][0]
    assert sprite["name"] == "Sprite"
    assert sprite["children"][0]["name"] == "Shape"


def test_parse_rejects_format2():
    with pytest.raises(SceneFormatError, match="format"):
        parse_tscn('[gd_scene format=2]\n\n[node name="X" type="Node2D"]\n')


def test_find_normalizes_root_prefix():
    sf = parse_tscn(_read("main.tscn"))
    assert sf.find("Player") is sf.find("Main/Player")   # 容忍根名前缀
    with pytest.raises(KeyError):
        sf.find("NotThere")


# ---------- 序列化黄金测试 ----------

def test_serialize_roundtrip():
    for name in ("player.tscn", "enemy.tscn", "main.tscn"):
        text = _read(name)
        assert SceneFile.serialize(parse_tscn(text)) == text, name


def test_serialize_roundtrip_nested():
    assert SceneFile.serialize(parse_tscn(NESTED)) == NESTED


# ---------- 结构化编辑 ----------

def test_add_set_connect_remove_roundtrip():
    sf = parse_tscn(_read("main.tscn"))
    sf.add_node(".", SceneNode(name="Trap", type="Area2D"))
    sf.set_prop("Trap", "position", "Vector2(10, 10)")
    sf.connect_signal("body_entered", "Trap", ".", "_on_trap")
    out = sf.serialize()

    sf2 = parse_tscn(out)                            # 序列化结果可再解析
    trap = sf2.find("Trap")
    assert trap.type == "Area2D"
    assert trap.props["position"] == "Vector2(10, 10)"
    assert any(c["from"] == "Trap" and c["to"] == "."
               for c in sf2.connections)
    assert SceneFile.serialize(parse_tscn(out)) == out   # 编辑后仍稳定


def test_add_node_with_script_resolves_resource():
    sf = parse_tscn(_read("main.tscn"))
    rid = sf.resource_for("Script", "res://trap.gd")
    assert rid == "4_trap"                           # 已有 1/2/3，新资源编号 4
    assert sf.resource_for("Script", "res://trap.gd") == rid   # 第二次复用
    sf.add_node(".", SceneNode(name="Trap", type="Area2D", script=rid))
    assert "res://trap.gd" in sf.serialize()


def test_add_node_guards():
    sf = parse_tscn(_read("main.tscn"))
    with pytest.raises(KeyError):                    # 父节点不存在
        sf.add_node("Nope", SceneNode(name="X", type="Node"))
    with pytest.raises(ValueError):                  # 同名冲突
        sf.add_node(".", SceneNode(name="Player", type="Node"))
    inst = sf.resource_for("PackedScene", "res://player.tscn")
    with pytest.raises(ValueError):                  # 实例节点不能带 type
        sf.add_node(".", SceneNode(name="P2", type="Node2D", instance_of=inst))


def test_remove_node_removes_subtree_and_connections():
    sf = parse_tscn(NESTED)
    assert sf.remove_node("Body") == 3               # Body/Sprite/Shape 全删
    assert [n.name for n in sf.nodes] == ["World"]

    sf2 = parse_tscn(_read("main.tscn"))
    assert sf2.remove_node("Player") == 1
    assert sf2.connections == []                     # 连线引用者被删 → 连线同删


def test_connect_signal_dedupes_and_validates():
    sf = parse_tscn(_read("main.tscn"))
    sf.connect_signal("health_changed", "Player", ".", "_on_player_health_changed")
    assert len(sf.connections) == 1                  # 重复连线跳过
    with pytest.raises(KeyError):
        sf.connect_signal("sig", "Ghost", ".", "_m")  # from 必须存在
