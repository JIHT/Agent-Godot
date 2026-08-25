"""tests/test_memory/test_profile.py —— M08 §4 步骤 4：项目画像事件流。"""
from __future__ import annotations

from agent_godot.memory import (ProfileEvent, ProfileManager, ProjectProfile,
                                MemoryStore)

from .conftest import make_store


async def test_scene_added_updates_inventory():
    """scene_added → inventory 追加。"""
    store = make_store()
    mgr = ProfileManager(store)
    await mgr.apply_event("p", ProfileEvent(
        type="scene_added", data={"path": "player.tscn", "type": "CharacterBody2D"}))
    profile = await mgr.get("p")
    assert profile.scene_inventory["player.tscn"] == "CharacterBody2D"
    store.close()


async def test_scene_removed_drops_from_inventory():
    """scene_removed → inventory 删除。"""
    store = make_store()
    mgr = ProfileManager(store)
    await mgr.apply_event("p", ProfileEvent(
        type="scene_added", data={"path": "enemy.tscn", "type": ""}))
    await mgr.apply_event("p", ProfileEvent(
        type="scene_removed", data={"path": "enemy.tscn"}))
    profile = await mgr.get("p")
    assert "enemy.tscn" not in profile.scene_inventory
    store.close()


async def test_version_detected_overwrites():
    """version_detected → godot_version 覆盖（upsert 语义）。"""
    store = make_store()
    mgr = ProfileManager(store)
    await mgr.apply_event("p", ProfileEvent(
        type="version_detected", data={"version": "4.2"}))
    await mgr.apply_event("p", ProfileEvent(
        type="version_detected", data={"version": "4.3"}))
    profile = await mgr.get("p")
    assert profile.godot_version == "4.3"     # 覆盖不是追加
    store.close()


async def test_naming_convention_upsert():
    """naming_convention → 约定字典 upsert。"""
    store = make_store()
    mgr = ProfileManager(store)
    await mgr.apply_event("p", ProfileEvent(
        type="naming_convention",
        data={"key": "signal_callback", "value": "_on_x_y"}))
    profile = await mgr.get("p")
    assert profile.naming_conventions["signal_callback"] == "_on_x_y"
    store.close()


async def test_milestone_keeps_last_10():
    """milestone 追加，保留最近 10 条。"""
    store = make_store()
    mgr = ProfileManager(store)
    for i in range(15):
        await mgr.apply_event("p", ProfileEvent(
            type="milestone", data={"description": f"里程碑 {i}"}))
    profile = await mgr.get("p")
    assert len(profile.recent_milestones) == 10
    assert "里程碑 14" in profile.recent_milestones[-1]    # 最后一条保留
    assert "里程碑 5" in profile.recent_milestones[0]      # 第 6 条是最早保留的
    # 0~4 被淘汰
    all_text = " ".join(profile.recent_milestones)
    assert "里程碑 0" not in all_text
    assert "里程碑 4" not in all_text
    store.close()


async def test_profile_persists_across_instances():
    """画像持久化：新 ProfileManager 实例能读到旧数据。"""
    store = make_store()
    mgr1 = ProfileManager(store)
    await mgr1.apply_event("p", ProfileEvent(
        type="version_detected", data={"version": "4.3"}))
    # 新实例（同 store）
    mgr2 = ProfileManager(store)
    profile = await mgr2.get("p")
    assert profile.godot_version == "4.3"
    store.close()


async def test_profile_render():
    """ProjectProfile.render 输出 XML 标签格式。"""
    profile = ProjectProfile(
        godot_version="4.3",
        naming_conventions={"indent": "tabs"},
        scene_inventory={"player.tscn": "CharacterBody2D"},
        recent_milestones=["v0.1 发布"])
    rendered = profile.render()
    assert "<project_profile>" in rendered
    assert "</project_profile>" in rendered
    assert "4.3" in rendered
    assert "tabs" in rendered
    assert "player.tscn" in rendered


async def test_profile_render_empty():
    """空画像渲染返回空串。"""
    profile = ProjectProfile()
    assert profile.render() == ""


async def test_apply_events_batch():
    """批量事件应用减少 IO。"""
    store = make_store()
    mgr = ProfileManager(store)
    events = [
        ProfileEvent(type="version_detected", data={"version": "4.3"}),
        ProfileEvent(type="scene_added", data={"path": "a.tscn", "type": ""}),
        ProfileEvent(type="naming_convention",
                     data={"key": "indent", "value": "tabs"}),
    ]
    await mgr.apply_events("p", events)
    profile = await mgr.get("p")
    assert profile.godot_version == "4.3"
    assert "a.tscn" in profile.scene_inventory
    assert profile.naming_conventions["indent"] == "tabs"
    store.close()
