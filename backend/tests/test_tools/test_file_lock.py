"""tests/test_tools/test_file_lock.py —— 乐观锁单测（M04 §5）。"""
from __future__ import annotations

from pathlib import Path

from agent_godot.tools import ErrorKind, OptimisticFileStore, sha16


async def test_optimistic_conflict_detected(tmp_path: Path):
    """read 后文件被外部修改 → write 返回 CONFLICT 且 hint 提示重读。"""
    (tmp_path / "a.gd").write_text("原始内容", encoding="utf-8")
    store = OptimisticFileStore(tmp_path)

    content, h = await store.read("a.gd")
    assert content == "原始内容" and h == sha16("原始内容")

    Path(tmp_path / "a.gd").write_text("用户手改的内容", encoding="utf-8")  # 模拟外部修改

    r = await store.write("a.gd", "模型的新内容", h)
    assert not r.ok
    assert r.error.kind is ErrorKind.CONFLICT
    assert "read_file" in (r.error.hint or "")


async def test_write_success_roundtrip(tmp_path: Path):
    """正常流程：read → write（hash 匹配）→ 再 read 内容已更新。"""
    (tmp_path / "b.txt").write_text("v1", encoding="utf-8")
    store = OptimisticFileStore(tmp_path)

    _, h1 = await store.read("b.txt")
    r = await store.write("b.txt", "v2", h1)
    assert r.ok
    content, h2 = await store.read("b.txt")
    assert content == "v2" and h2 == sha16("v2") and h2 != h1


async def test_write_new_file_with_empty_hash(tmp_path: Path):
    """新文件：expect_hash 传空字符串即可创建。"""
    store = OptimisticFileStore(tmp_path)
    r = await store.write("new_dir/c.txt", "hello", "")
    assert r.ok
    assert (tmp_path / "new_dir" / "c.txt").read_text(encoding="utf-8") == "hello"


async def test_read_missing_file_returns_empty_pair(tmp_path: Path):
    """文件不存在 → ("", "")——hash 为空串即'不存在'的信号。"""
    store = OptimisticFileStore(tmp_path)
    content, h = await store.read("nope.txt")
    assert content == "" and h == ""
