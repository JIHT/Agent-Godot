"""项目结构图：建图 / 增量替换 / 影响分析 / 死信号（M11 §5 验收）。"""
from __future__ import annotations

from pathlib import Path

from agent_godot.graphrag import ImpactEdge, ProjectGraphSync


async def test_full_sync_matches_tscn(driver, sample_project):
    """验收①：节点数 = 解析器节点总数；LISTENS 边数 = connection 总数。"""
    sync = ProjectGraphSync(driver)
    total = await sync.full_sync("m06", sample_project)
    # SceneNode: main(4) + player(1) + enemy(2) = 7；Script = 3
    assert total == 10
    assert driver.count("SceneNode") == 7
    assert driver.count("Script") == 3
    # connection 总数 = 1（main.tscn 的 health_changed）
    assert driver.count_edges("LISTENS") == 1
    assert await sync.graph_exists("m06")


async def test_upsert_file_replaces_subgraph(graph, driver, sample_project,
                                             main_tscn):
    """验收②：改 main.tscn 后旧子图消失、新子图正确、无重复残留。"""
    # 改文件：去 Camera、去连线、加 HUD 节点
    new_tscn = main_tscn.replace(
        '[node name="Camera" type="Camera2D" parent="."]', ""
    ).replace(
        '[connection signal="health_changed" from="Player" to="." '
        'method="_on_player_health_changed"]',
        '[connection signal="game_over" from="Player" to="." '
        'method="_on_game_over"]',
    ) + '\n[node name="HUD" type="CanvasLayer" parent="."]\n'
    (sample_project / "main.tscn").write_text(new_tscn, encoding="utf-8")

    await graph.upsert_file("m06", Path("main.tscn"))

    # 旧子图消失：Camera 没了；新节点进图；main.tscn 的节点 = 4
    # （Main/Player/Enemy/HUD；player/enemy.tscn 的 3 个节点不受影响）
    assert driver.count("SceneNode") == 7
    assert driver.count("SceneNode", name="Camera") == 0
    assert driver.count("SceneNode", name="HUD") == 1
    main_nodes = [n for n in driver._nodes
                  if n["label"] == "SceneNode"
                  and n["props"]["path"].startswith("res://main.tscn#")]
    assert len(main_nodes) == 4
    # 无重复残留：场景节点 path 唯一
    scene_paths = [p for p in
                   (n["props"]["path"] for n in driver._nodes
                    if n["label"] == "SceneNode")]
    assert len(scene_paths) == len(set(scene_paths))
    # 连线换新：health_changed 的监听没了，game_over 有了
    assert driver.count_edges("LISTENS") == 1
    impacts = await graph.impact_of_signal("m06", "health_changed")
    assert impacts == []
    game_over = await graph.impact_of_signal("m06", "game_over")
    assert game_over and game_over[0].node == "Main"


async def test_impact_of_signal(graph):
    """验收③：两跳影响分析返回 监听场景 + 处理脚本。"""
    impacts = await graph.impact_of_signal("m06", "health_changed")
    assert len(impacts) == 1
    imp = impacts[0]
    assert isinstance(imp, ImpactEdge)
    assert imp.node == "Main"
    assert imp.script == "res://main.gd"
    assert imp.method == "_on_player_health_changed"


async def test_dead_signals(graph, driver, sample_project):
    """死信号体检：声明了没人监听的信号。"""
    # m06 现状：player.gd 声明 health_changed 且 main.tscn 监听 → 不死
    assert "health_changed" not in await graph.dead_signals("m06")

    # 造一个死信号：enemy.gd 声明 boss_died，无人监听
    (sample_project / "enemy.gd").write_text(
        ENEMY_DEAD_GD, encoding="utf-8")
    await graph.upsert_file("m06", Path("enemy.gd"))
    dead = await graph.dead_signals("m06")
    assert dead == ["boss_died"]


ENEMY_DEAD_GD = """extends CharacterBody2D

signal boss_died

var speed: float = 120.0
"""


async def test_signals_of_project_and_trace_dictionary(graph):
    """信号词典（fusion.trace 的数据源）。"""
    signals = await graph.signals_of_project("m06")
    assert signals == ["health_changed"]


async def test_project_graph_not_listens_without_connection(driver,
                                                            sample_project):
    """.player.gd 单独 upsert（无场景连线）→ 信号必死。"""
    sync = ProjectGraphSync(driver, root=sample_project)
    await sync.upsert_file("m06", Path("player.gd"))
    assert await sync.dead_signals("m06") == ["health_changed"]
