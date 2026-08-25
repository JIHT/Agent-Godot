"""tools/godot/checkpoints.py —— 任务级检查点聚合（M06 §1.4 / §4 步骤 6）

游戏存档系统：Agent 每次动手改文件前先存档（快照该文件）；一个任务的所有
存档打包成一个存档槽（task_id）；读档（回滚）时**从最后一格往回放**——
先撤销最近的改动、再撤销更早的，最终回到动手前（同一文件改三次时，
正序回放会停在中间态，逆序才回到原点；数据库 undo log 同理）。

为什么不用 git stash：①污染用户 commit 历史 ②语义重（分支/stash 对文件级
快照是过度设计）③不可控副作用（hooks/合并冲突）——Agent 的失误由 Agent
自己的机制兜底。

存储布局：.agent_godot/checkpoints/{task_id}/{seq:03d}_{path_hash}/{filename}
+ manifest.json（seq/path/hash/existed/reason/ts，临时文件+rename 原子写）。
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..file_lock import sha16


@dataclass
class CheckpointInfo:
    """一个任务检查点的概要（list() 的返回项）。"""
    task_id: str
    created_at: float
    snapshots: int
    reason: str = ""


def _task_dir_name() -> str:
    """可排序的 task_id：时间戳前缀（list/rewind 按它定先后）+ uuid 防同秒碰撞。"""
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


class CheckpointStore:
    """磁盘版检查点仓库（M04 file_lock 内存 snapshot 的升级——支持跨进程回滚）。"""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()
        self.root = self.project_root / ".agent_godot" / "checkpoints"

    # ---------- manifest（清单读写） ----------

    def _manifest_path(self, task_id: str) -> Path:
        return self.root / task_id / "manifest.json"

    def _manifest(self, task_id: str) -> list[dict]:
        p = self._manifest_path(task_id)
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []                     # 损坏的清单当空处理（半份清单不能炸回滚）

    def _write_manifest(self, task_id: str, records: list[dict]) -> None:
        """原子写：临时文件 + os.replace——进程中途被杀不能留半个清单。"""
        p = self._manifest_path(task_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(p)

    def _snap_dir(self, task_id: str, seq: int, rel_posix: str) -> Path:
        return self.root / task_id / f"{seq:03d}_{sha16(rel_posix)}"

    # ---------- 快照 / 回滚 ----------

    def snapshot(self, path: Path, task_id: str, reason: str = "") -> str:
        """写前快照一个文件，返回 "{task_id}:{seq}"。

        ★ "创建前不存在"的文件也要记录（existed=False）——否则回滚留下幽灵文件。
        ★ copy2 保 mtime——回滚后乐观锁/缓存校验不误报"文件被外部修改"。
        """
        path = Path(path).resolve()
        rel = path.relative_to(self.project_root)
        records = self._manifest(task_id)
        seq = len(records) + 1
        snap_dir = self._snap_dir(task_id, seq, rel.as_posix())
        existed = path.exists()
        if existed:
            snap_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, snap_dir / path.name)
        records.append({"seq": seq, "path": rel.as_posix(), "existed": existed,
                        "hash": sha16(path.read_text(encoding="utf-8",
                                                     errors="replace"))
                        if existed else None,
                        "reason": reason, "ts": time.time()})
        self._write_manifest(task_id, records)
        return f"{task_id}:{seq}"

    def rollback(self, task_id: str) -> list[str]:
        """逆序回放 manifest：existed 的拷回（保 mtime），不存在的删除。"""
        restored: list[str] = []
        for rec in reversed(self._manifest(task_id)):      # ★ 逆序
            dst = self.project_root / rec["path"]
            if rec["existed"]:
                src = self._snap_dir(task_id, rec["seq"], rec["path"])
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src / dst.name, dst)
            else:
                dst.unlink(missing_ok=True)                # 当初不存在的文件，回滚=删除
            if rec["path"] not in restored:
                restored.append(rec["path"])
        return restored


class TaskCheckpoints:
    """任务级聚合：open_task 开存档槽，snapshot 逐文件存档，rollback 整槽读档。"""

    def __init__(self, project_root: Path):
        self.store = CheckpointStore(project_root)
        self._current: str | None = None

    def open_task(self) -> str:
        task_id = _task_dir_name()
        (self.store.root / task_id).mkdir(parents=True, exist_ok=True)
        self._current = task_id
        return task_id

    @property
    def current(self) -> str | None:
        return self._current

    def snapshot(self, path: Path, reason: str = "") -> str:
        """快照（没开过任务则自动开——工具层不必关心槽位管理）。"""
        if self._current is None:
            self.open_task()
        return self.store.snapshot(path, self._current, reason)

    def rollback(self, task_id: str | None = None) -> list[str]:
        """回滚整个任务；task_id=None 回滚最新任务。返回恢复的文件清单。"""
        if task_id is None:
            infos = self.list()
            if not infos:
                return []
            task_id = infos[-1].task_id
        return self.store.rollback(task_id)

    def list(self) -> list[CheckpointInfo]:
        """全部任务检查点（按时间正序）。"""
        infos: list[CheckpointInfo] = []
        if not self.store.root.exists():
            return infos
        for d in self.store.root.iterdir():
            if not d.is_dir():
                continue
            records = self._safe_records(d.name)
            if not records:
                continue
            infos.append(CheckpointInfo(
                task_id=d.name,
                created_at=records[0].get("ts", 0.0),
                snapshots=len(records),
                reason=records[0].get("reason", "")))
        # 目录名含时间戳但同秒任务按 uuid 排序会乱序——以首条快照的 ts 为准
        infos.sort(key=lambda i: (i.created_at, i.task_id))
        return infos

    def _safe_records(self, task_id: str) -> list[dict]:
        return self.store._manifest(task_id)
