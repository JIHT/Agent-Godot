"""memory/profile.py —— 项目结构化档案（M08 §1.1 / §4 步骤 4）

项目画像不是记忆表的一行——独立结构化文档（可整体替换），直查不检索。
混进向量库失去"直查"能力。

事件流 upsert 语义：scene_added→inventory 更新 / version_detected→覆盖 /
milestone→追加（保留最近 10 条）。无"遗忘"只有"覆盖"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from .store import MemoryStore


@dataclass
class ProjectProfile:
    """项目结构化档案：GDScript 版本 / 命名约定 / 场景清单 / 近期里程碑。"""
    godot_version: str | None = None
    naming_conventions: dict = field(default_factory=dict)
    scene_inventory: dict[str, str] = field(default_factory=dict)
    recent_milestones: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "godot_version": self.godot_version,
            "naming_conventions": dict(self.naming_conventions),
            "scene_inventory": dict(self.scene_inventory),
            "recent_milestones": list(self.recent_milestones),
        }

    @staticmethod
    def from_dict(d: dict | None) -> ProjectProfile:
        if not d:
            return ProjectProfile()
        return ProjectProfile(
            godot_version=d.get("godot_version"),
            naming_conventions=dict(d.get("naming_conventions") or {}),
            scene_inventory=dict(d.get("scene_inventory") or {}),
            recent_milestones=list(d.get("recent_milestones") or []),
        )

    def render(self) -> str:
        """渲染为可注入上下文的文本（画像区独立于 memory 分区）。"""
        lines = ["<project_profile>"]
        if self.godot_version:
            lines.append(f"Godot 版本: {self.godot_version}")
        if self.naming_conventions:
            convs = "; ".join(f"{k}={v}" for k, v in self.naming_conventions.items())
            lines.append(f"命名约定: {convs}")
        if self.scene_inventory:
            scenes = ", ".join(self.scene_inventory.keys())
            lines.append(f"场景清单: {scenes}")
        if self.recent_milestones:
            lines.append("近期里程碑:")
            for m in self.recent_milestones:
                lines.append(f"  - {m}")
        lines.append("</project_profile>")
        return "\n".join(lines) if len(lines) > 2 else ""


@dataclass
class ProfileEvent:
    """画像更新事件（全部 upsert 语义）。"""
    type: Literal["scene_added", "scene_removed", "version_detected",
                  "naming_convention", "milestone"]
    data: dict


class ProfileManager:
    """事件驱动 upsert：apply_event 按类型分支更新档案。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    async def get(self, project_id: str) -> ProjectProfile:
        data = await self.store.get_profile_data(project_id)
        return ProjectProfile.from_dict(data)

    async def apply_event(self, project_id: str,
                          event: ProfileEvent) -> ProjectProfile:
        """应用事件 → 返回更新后的 profile（全部 upsert 语义）。"""
        profile = await self.get(project_id)
        t = event.type
        d = event.data
        if t == "scene_added":
            profile.scene_inventory[d["path"]] = d.get("type", "")
        elif t == "scene_removed":
            profile.scene_inventory.pop(d["path"], None)
        elif t == "version_detected":
            profile.godot_version = d["version"]
        elif t == "naming_convention":
            profile.naming_conventions[d["key"]] = d["value"]
        elif t == "milestone":
            stamp = time.strftime("%Y-%m-%d")
            profile.recent_milestones.append(f"[{stamp}] {d['description']}")
            profile.recent_milestones = profile.recent_milestones[-10:]  # 保留最近 10 条
        await self.store.save_profile_data(project_id, profile.to_dict())
        return profile

    async def apply_events(self, project_id: str,
                           events: list[ProfileEvent]) -> ProjectProfile:
        """批量应用（减少 IO）。"""
        profile = await self.get(project_id)
        for event in events:
            t, d = event.type, event.data
            if t == "scene_added":
                profile.scene_inventory[d["path"]] = d.get("type", "")
            elif t == "scene_removed":
                profile.scene_inventory.pop(d["path"], None)
            elif t == "version_detected":
                profile.godot_version = d["version"]
            elif t == "naming_convention":
                profile.naming_conventions[d["key"]] = d["value"]
            elif t == "milestone":
                stamp = time.strftime("%Y-%m-%d")
                profile.recent_milestones.append(f"[{stamp}] {d['description']}")
                profile.recent_milestones = profile.recent_milestones[-10:]
        await self.store.save_profile_data(project_id, profile.to_dict())
        return profile


__all__ = ["ProfileEvent", "ProfileManager", "ProjectProfile"]
