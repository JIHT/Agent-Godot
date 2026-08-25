"""tests/test_session/test_rewind.py —— M09 §1.4 / §5：三联动回滚 + 命名存档。

/rewind N：丢掉最近 N 轮对话事件 + 回滚这 N 轮产生的所有文件检查点——
只回滚一半是灾难级 bug，这里同时断言对话与文件两个世界。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_godot.session import (NamedCheckpoints, SessionManager,
                                 SessionState)
from agent_godot.session.rewind import split_before
from agent_godot.session.state import (AssistantMsg, SessionCreated, ToolDone,
                                       UserInput)


def _mk_events(n_turns: int) -> list:
    events: list = [SessionCreated(project_id="p")]
    for i in range(n_turns):
        events.append(UserInput(text=f"第 {i + 1} 轮"))
        events.append(AssistantMsg(content=f"回答 {i + 1}"))
        events.append(ToolDone(call_id=f"c{i}", tool="mark", ok=True, summary="ok"))
    return events


def test_split_before():
    events = _mk_events(3)                      # 1 + 3*3 = 10 个事件
    kept, dropped = split_before(events, 1)
    assert len(dropped) == 3                    # 最后一轮的 3 个事件
    assert kept == events[:len(events) - 3]
    # 丢掉超过全部轮数 → 全部丢弃
    kept, dropped = split_before(events, 99)
    assert kept == [] and len(dropped) == 10


async def _setup_two_turn_session(tmp_path: Path):
    """两轮写文件任务：每轮真实通过 M06 检查点改一次文件。"""
    manager = SessionManager(tmp_path, db_path=tmp_path / "sessions.db")
    target = tmp_path / "player.gd"
    target.write_text("v0", encoding="utf-8")

    s = await manager.create()
    ck = manager.checkpoints
    for i, content in enumerate(["v1", "v2"], start=1):
        s.record(UserInput(text=f"改成 {content}"))
        s.record(AssistantMsg(content=content, tool_calls=[
            {"id": f"c{i}", "name": "write_file",
             "arguments": f'{{"path": "player.gd", "content": "{content}"}}'}]))
        ck.open_task()                               # 每轮一个任务槽（M06 语义）
        ck.snapshot(target, reason=f"write round {i}")
        target.write_text(content, encoding="utf-8")
        s.record(ToolDone(call_id=f"c{i}", tool="write_file", ok=True,
                          summary=f"written {content}"))
    return manager, s, target


async def test_rewind_reverts_dialog_and_files(tmp_path: Path):
    """两轮写文件任务 → rewind(2) → 文件内容恢复且会话纪要回到第 0 轮。"""
    manager, s, target = await _setup_two_turn_session(tmp_path)
    assert target.read_text(encoding="utf-8") == "v2"

    report = await manager.rewind(s.session_id, 2)

    assert target.read_text(encoding="utf-8") == "v0"      # 文件回到动手前
    assert report.files_restored == ["player.gd"]
    assert len(report.task_ids) == 2                        # 两轮的任务全回滚
    restored = await manager.resume(s.session_id)
    assert restored.turns() == 0                            # 对话回到第 0 轮
    assert restored.messages == []
    assert restored.state is SessionState.ACTIVE
    assert restored.rolled_back_turns == 2


async def test_rewind_one_turn_only(tmp_path: Path):
    """rewind(1) 只回滚最近一轮（v2→v1），更早的轮次保留。"""
    manager, s, target = await _setup_two_turn_session(tmp_path)
    report = await manager.rewind(s.session_id, 1)
    assert target.read_text(encoding="utf-8") == "v1"
    assert len(report.task_ids) == 1
    restored = await manager.resume(s.session_id)
    assert restored.turns() == 1
    assert restored.messages[0].content == "改成 v1"


async def test_rewind_sequential_semantics(tmp_path: Path):
    """rewind 1 再 rewind 1 ≠ rewind 2：第二次基准是"当前（已缩短的）历史"。"""
    manager, s, target = await _setup_two_turn_session(tmp_path)
    await manager.rewind(s.session_id, 1)          # v2 → v1，剩 1 轮
    report = await manager.rewind(s.session_id, 1) # 再退 1 轮 → v0，剩 0 轮
    assert target.read_text(encoding="utf-8") == "v0"
    assert report.kept_turns == 0


async def test_rewind_no_files_touched_when_no_checkpoints(tmp_path: Path):
    """dropped 窗口里没有任务检查点时：只截断对话，文件系统不动。"""
    manager = SessionManager(tmp_path, db_path=tmp_path / "sessions.db")
    s = await manager.create()
    s.record(UserInput(text="纯聊天"))
    s.record(AssistantMsg(content="好的"))
    report = await manager.rewind(s.session_id, 1)
    assert report.files_restored == []
    assert report.task_ids == []


async def test_named_checkpoints_save_and_restore(tmp_path: Path):
    """/checkpoint save "重构前" → 继续改 → restore 读档回到存档点。"""
    manager, s, target = await _setup_two_turn_session(tmp_path)
    nc = NamedCheckpoints(tmp_path)

    await manager.checkpoint_named(s.session_id, "重构前")
    assert [i["name"] for i in nc.list()] == ["重构前"]

    # 存档后继续破坏性修改（新一轮任务）
    ck = manager.checkpoints
    s.record(UserInput(text="第三轮"))
    ck.open_task()
    ck.snapshot(target, reason="round 3")
    target.write_text("v3", encoding="utf-8")

    files = nc.restore("重构前")
    assert files == ["player.gd"]
    assert target.read_text(encoding="utf-8") == "v2"      # 回到存档时的世界


async def test_named_checkpoint_missing_raises(tmp_path: Path):
    nc = NamedCheckpoints(tmp_path)
    with pytest.raises(KeyError):
        nc.restore("不存在的存档")
