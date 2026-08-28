"""M11 GraphRAG 测试公共 fixtures。

样例项目 = lab/m06/sample 的内容内嵌（教学版惯例：测试不依赖 lab 目录，
自己的数据自己带）。文档路 = M10 的 FakeEmbedding/内存索引/BM25 全家桶。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_godot.graphrag import (InMemoryGraphDriver, ProjectGraphSync,
                                  GraphVectorFusion)
from agent_godot.rag import (BM25Index, Chunk, FakeEmbeddingService,
                              HybridRetriever, InMemoryVectorIndex)

# ---- lab/m06 样例项目（文件内容内嵌，和源文件保持一致） ----

MAIN_TSCN = """[gd_scene load_steps=4 format=3 uid="uid://m06samplemain"]

[ext_resource type="Script" path="res://main.gd" id="1_main"]
[ext_resource type="PackedScene" path="res://player.tscn" id="2_player"]
[ext_resource type="PackedScene" path="res://enemy.tscn" id="3_enemy"]

[node name="Main" type="Node2D"]
script = ExtResource("1_main")

[node name="Player" parent="." instance=ExtResource("2_player")]

[node name="Enemy" parent="." instance=ExtResource("3_enemy")]

[node name="Camera" type="Camera2D" parent="."]

[connection signal="health_changed" from="Player" to="." method="_on_player_health_changed"]
"""

PLAYER_TSCN = """[gd_scene load_steps=2 format=3 uid="uid://m06sampleplayer"]

[ext_resource type="Script" path="res://player.gd" id="1_player"]

[node name="Player" type="CharacterBody2D"]
script = ExtResource("1_player")
"""

ENEMY_TSCN = """[gd_scene load_steps=2 format=3 uid="uid://m06sampleenemy"]

[ext_resource type="Script" path="res://enemy.gd" id="1_enemy"]

[node name="Enemy" type="CharacterBody2D"]
script = ExtResource("1_enemy")

[node name="Sprite" type="Sprite2D" parent="."]
modulate = Color(1, 0.6, 0.6, 1)
"""

MAIN_GD = """extends Node2D


func _on_player_health_changed(new_health: int) -> void:
\tprint("Player health: ", new_health)
"""

PLAYER_GD = """extends CharacterBody2D

signal health_changed(new_health: int)

var speed: float = 200.0
var health: int = 100


func _physics_process(delta: float) -> void:
\tvar direction := Input.get_axis("move_left", "move_right")
\tvelocity.x = direction * speed
\tmove_and_slide()


func take_damage(amount: int) -> void:
\thealth = max(0, health - amount)
\thealth_changed.emit(health)
"""

ENEMY_GD = """extends CharacterBody2D

var speed: float = 120.0
var direction: int = 1

func _physics_process(delta: float) -> void:
\tposition.x += direction * speed * delta
"""

# 融合测试的文档路语料（M10 conftest 的 GODOT_MD 摘要版）
DOC_TEXTS = [
    ("docs/physics.md", "CharacterBody2D 是 2D 游戏中最常用的角色节点。"
     "move_and_slide 返回布尔表示本帧是否碰撞，可用于落地检测。"),
    ("docs/area2d.md", "body_entered 信号在 monitoring 与 monitorable "
     "同时为真时触发。Area2D 继承自 CollisionObject2D。"),
]


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    files = {"main.tscn": MAIN_TSCN, "player.tscn": PLAYER_TSCN,
             "enemy.tscn": ENEMY_TSCN, "main.gd": MAIN_GD,
             "player.gd": PLAYER_GD, "enemy.gd": ENEMY_GD}
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "project.godot").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def driver() -> InMemoryGraphDriver:
    return InMemoryGraphDriver()


@pytest.fixture
def main_tscn() -> str:
    return MAIN_TSCN


@pytest.fixture
def player_gd() -> str:
    return PLAYER_GD


@pytest.fixture
async def graph(driver, sample_project) -> ProjectGraphSync:
    sync = ProjectGraphSync(driver)
    await sync.full_sync("m06", sample_project)
    return sync


@pytest.fixture
async def hybrid():
    embedder = FakeEmbeddingService(dim=64)
    vec = InMemoryVectorIndex()
    bm25 = BM25Index()
    chunks = [Chunk(text=t, source=src, heading="", start=1,
                    doc_id=f"doc{i}", kind="md", seq=0)
              for i, (src, t) in enumerate(DOC_TEXTS)]
    embs = await embedder.embed_documents([c.text for c in chunks])
    vec.upsert(chunks, embs)
    bm25.build(chunks)
    return HybridRetriever(vec, bm25, embedder, top_per_route=10)
