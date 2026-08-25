"""session/rewind.py —— 对话-文件-记忆三联动回滚（M09 §1.4 / §4 步骤 6）

游戏多存档系统：读档要**整个世界回到当时**——
- 对话状态：事件截断（kept 回放重建，黑匣子里丢掉最近 N 轮）
- 文件系统：M06 TaskCheckpoints.rollback 联动（dropped 窗口内的任务全回滚）
- 记忆：Rewind 落盘后 Extractor 的输入按 kept 事件构造（M08 消费本事件）
只回滚一半是灾难级 bug：漏对话=模型精神分裂，漏文件=乐观锁冲突，
漏记忆=未来会话引用被否定的决策。

多次 rewind 组合语义：rewind 3 再 rewind 2 ≠ rewind 5——第二次的基准是
"当前（已缩短的）历史"，语义已在 split_before 按当前事件流计算。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .manager import Session
from .state import Rewind, SessionCreated, UserInput


@dataclass
class RewindReport:
    """/rewind 的回执：丢了什么、保了什么、文件回滚了哪些。"""
    turns: int
    kept_turns: int
    dropped_events: int
    files_restored: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)


def split_before(events: list, drop_turns: int) -> tuple[list, list]:
    """按轮边界切分：丢掉最近 drop_turns 轮（含其中的全部事件）。"""
    ui_idx = [i for i, e in enumerate(events) if isinstance(e, UserInput)]
    if drop_turns >= len(ui_idx):
        return [], list(events)
    cut = ui_idx[len(ui_idx) - drop_turns]
    return list(events[:cut]), list(events[cut:])


async def rewind_to(manager, session_id: str, turns: int) -> RewindReport:
    """§1.3 ③：split_before → checkpoints.rollback 联动文件 → append RewindEvent → kept 回放。"""
    if turns <= 0:
        raise ValueError("rewind 轮数必须 >= 1")
    events = manager.store.load(session_id)
    if not events:
        raise KeyError(f"会话不存在: {session_id}")
    kept, dropped = split_before(events, turns)

    # ---- 文件联动：dropped 窗口内创建的任务检查点全部回滚（逆时间序） ----
    # 跳过本会话此前 rewind 已回滚过的任务——rewind 3 再 rewind 2 的基准
    # 是"当前（已缩短的）历史"，已回滚的任务再滚一次会把文件退过头（§1.4 易错点②）
    already_rolled = {tid for e in events if isinstance(e, Rewind)
                      for tid in e.task_ids}
    ts_from = dropped[0].ts if dropped else 0.0
    task_ids = [i.task_id for i in manager.checkpoints.list()
                if i.created_at >= ts_from and i.task_id not in already_rolled]
    files_restored: list[str] = []
    for tid in reversed(task_ids):
        for f in manager.checkpoints.rollback(tid):
            if f not in files_restored:              # 同文件多任务去重保序
                files_restored.append(f)

    # ---- 对话联动：物理截断 + RewindEvent 落盘（审计：丢了哪些轮/回了哪些文件） ----
    manager.store.truncate(session_id, keep_seq=len(kept))
    s = Session(session_id)
    for e in kept:
        s.apply(e)
    rewind_ev = Rewind(turns=turns, files=files_restored, task_ids=task_ids)
    s.apply(rewind_ev)
    manager.store.append(session_id, len(kept) + 1, rewind_ev)
    manager._attach_persist(s)

    return RewindReport(
        turns=turns, kept_turns=s.turns(), dropped_events=len(dropped),
        files_restored=files_restored, task_ids=task_ids)


class NamedCheckpoints:
    """/checkpoint save "重构前" —— 命名存档（打 Boss 前手动留退路）。

    存档 = 时间戳 + 当时已有的任务检查点索引；读档 = 逆序回滚存档**之后**
    新建的全部任务（存档时已存在的快照是"存档世界"的一部分，回滚它们
    反而会退到更早）。容量上限 20 个，超出按最旧淘汰（膨胀治理）。
    """

    MAX_SAVES = 20

    def __init__(self, project_root: Path):
        self.root = Path(project_root) / ".agent_godot" / "named_checkpoints"

    def _path(self, name: str) -> Path:
        safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name).strip("_") or "save"
        return self.root / f"{safe}.json"

    def _task_checkpoints(self):
        from agent_godot.tools.godot.checkpoints import TaskCheckpoints
        return TaskCheckpoints(self.root.parent.parent)  # .agent_godot 上级=项目根

    def save(self, name: str, task_ids: list[str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(name).write_text(json.dumps({
            "name": name, "task_ids": task_ids, "ts": time.time()},
            ensure_ascii=False, indent=1), encoding="utf-8")
        self._evict_overflow()

    def restore(self, name: str) -> list[str]:
        """读档：逆序回滚存档之后新建的任务检查点，返回恢复的文件清单。"""
        p = self._path(name)
        if not p.exists():
            raise KeyError(f"命名存档不存在: {name}")
        info = json.loads(p.read_text(encoding="utf-8"))
        ts = info.get("ts", 0.0)
        ck = self._task_checkpoints()
        later = [i.task_id for i in ck.list() if i.created_at > ts]
        files: list[str] = []
        for tid in reversed(later):
            files.extend(ck.rollback(tid))
        return files

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        out = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def _evict_overflow(self) -> None:
        saves = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for p in saves[:-self.MAX_SAVES]:
            p.unlink(missing_ok=True)


# 事件类重导出便于外部引用（保持单一来源）
__all__ = ["RewindReport", "rewind_to", "split_before", "NamedCheckpoints",
           "Session", "UserInput", "SessionCreated", "Rewind"]
