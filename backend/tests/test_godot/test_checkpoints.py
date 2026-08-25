"""tests/test_godot/test_checkpoints.py —— 检查点快照与回滚（M06 §5）。

核心场景：多文件任务（改 2 个 + 新建 1 个）回滚后——改的恢复原样、新建的消失；
同一文件多次修改 → 逆序回放回到最初。
"""
import time
from pathlib import Path

from agent_godot.tools.godot.checkpoints import (CheckpointStore,
                                                 TaskCheckpoints)


def test_rollback_restores_modified_and_deletes_created(tmp_path: Path):
    existing = tmp_path / "existing.gd"
    existing.write_text("original", encoding="utf-8")

    ck = TaskCheckpoints(tmp_path)
    ck.open_task()
    ck.snapshot(existing, reason="before edit")      # 存档 v0
    existing.write_text("changed", encoding="utf-8")

    new_file = tmp_path / "new_file.gd"
    ck.snapshot(new_file, reason="before create")    # 存档"不存在"
    new_file.write_text("v1", encoding="utf-8")

    restored = ck.rollback()
    assert existing.read_text(encoding="utf-8") == "original"
    assert not new_file.exists()                     # 当初不存在的文件，回滚=删除
    assert "existing.gd" in restored and "new_file.gd" in restored


def test_rollback_reverse_order_same_file(tmp_path: Path):
    """同一文件改三次（v0→v1→v2→v3）：逆序回放才回到 v0（正序会停在中间态）。"""
    f = tmp_path / "a.gd"
    f.write_text("v0", encoding="utf-8")
    ck = TaskCheckpoints(tmp_path)
    ck.open_task()
    for i in (1, 2, 3):
        ck.snapshot(f)
        f.write_text(f"v{i}", encoding="utf-8")
    assert f.read_text(encoding="utf-8") == "v3"
    ck.rollback()
    assert f.read_text(encoding="utf-8") == "v0"


def test_rollback_preserves_mtime(tmp_path: Path):
    """copy2 保 mtime——回滚后乐观锁/缓存校验不误报"文件被外部修改"。"""
    f = tmp_path / "b.gd"
    f.write_text("v0", encoding="utf-8")
    mtime = f.stat().st_mtime_ns
    time.sleep(0.02)

    store = CheckpointStore(tmp_path)
    store.snapshot(f, "t1")
    f.write_text("v1", encoding="utf-8")
    assert f.stat().st_mtime_ns != mtime

    store.rollback("t1")
    assert f.read_text(encoding="utf-8") == "v0"
    assert f.stat().st_mtime_ns == mtime


def test_list_and_selective_rollback(tmp_path: Path):
    ck = TaskCheckpoints(tmp_path)
    t1 = ck.open_task()
    (tmp_path / "x.gd").write_text("x", encoding="utf-8")
    ck.snapshot(tmp_path / "x.gd")

    t2 = ck.open_task()                              # 第二个任务
    f2 = tmp_path / "y.gd"
    f2.write_text("y-old", encoding="utf-8")
    ck.snapshot(f2)
    f2.write_text("y-new", encoding="utf-8")

    infos = ck.list()
    assert [i.task_id for i in infos] == [t1, t2]    # 时间正序
    assert all(i.snapshots == 1 for i in infos)

    restored = ck.rollback(t1)                       # 指定回滚第一个任务
    assert restored == ["x.gd"]
    assert f2.read_text(encoding="utf-8") == "y-new"  # 第二个任务不受影响

    ck.rollback()                                    # 缺省 = 最新任务
    assert f2.read_text(encoding="utf-8") == "y-old"


def test_snapshot_outside_root_raises(tmp_path: Path):
    import pytest
    outside = tmp_path.parent / "outside.gd"
    store = CheckpointStore(tmp_path)
    with pytest.raises(ValueError):
        store.snapshot(outside, "t")
