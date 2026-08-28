"""M10 RAG 测试公共样本与 fixtures。"""
import pytest

from agent_godot.rag import (BM25Index, FakeEmbeddingService,
                             HybridRetriever, InMemoryVectorIndex,
                             ParsedDoc, StructureAwareChunker)

GODOT_MD = """# 角色物理

CharacterBody2D 是 2D 游戏中最常用的角色节点。

## move_and_slide

move_and_slide 返回值在 Godot 4.3 变为布尔：表示本帧是否发生碰撞，
可用于落地检测。常见用法是先 move_and_slide 再读 is_on_floor。

参数 slope_stop_min_velocity 控制斜坡停止的最小速度。

## Area2D 信号

body_entered 信号在 monitoring 与 monitorable 同时为真时触发。
monitored 属性控制对方能否监测到本节点。

### 碰撞报告

max_contacts_reported 属性控制每帧报告的最大碰撞数，
4.3 起默认值从 4 提升到 8。get_slide_collision 按索引取回碰撞信息。
"""

PLAYER_GD = """extends CharacterBody2D

class_name Player

signal hit(damage)

var speed := 300.0

# 速度向量积分：每帧位移
func _physics_process(delta):
\tvelocity.y += 980 * delta
\tmove_and_slide()

# 落地检测：4.3 起看返回值
func _check_landing():
\tif move_and_slide():
\t\tprint("landed")
"""


@pytest.fixture
def embedder():
    return FakeEmbeddingService(dim=64)


@pytest.fixture
def vec():
    return InMemoryVectorIndex()


@pytest.fixture
def bm25():
    return BM25Index()


@pytest.fixture
def retriever(embedder, vec, bm25):
    return HybridRetriever(vec, bm25, embedder, top_per_route=10)


@pytest.fixture
def md_doc():
    return ParsedDoc.make(source="docs/physics.md", kind="md", text=GODOT_MD)


@pytest.fixture
def gd_doc():
    return ParsedDoc.make(source="player.gd", kind="gdscript", text=PLAYER_GD)


@pytest.fixture
def chunker():
    return StructureAwareChunker()
