"""tests/test_godot/test_diff_apply.py —— Diff 逐 hunk 审批应用器（M06 §1.3 / §5 步骤 8）。

必须覆盖"跳过中间块"场景——offset 对齐是这类代码的坟场。
"""
import difflib

from agent_godot.tools.builtin.diff_tool import (Hunk, apply_hunks,
                                                 apply_unified, parse_hunks)

# 12 行旧文本，三处独立修改（间隔 4 行 > 2n，保证 difflib 产出 3 个独立 hunk）
OLD = "\n".join(f"line{i}" for i in range(1, 13)) + "\n"
NEW_LINES = [f"line{i}" for i in range(1, 13)]
NEW_LINES[1] = "TWO"        # line2
NEW_LINES[6] = "SEVEN"      # line7
NEW_LINES[11] = "TWELVE"    # line12
NEW = "\n".join(NEW_LINES) + "\n"


def _diff() -> str:
    return "".join(difflib.unified_diff(
        OLD.splitlines(keepends=True), NEW.splitlines(keepends=True),
        fromfile="a/f.txt", tofile="b/f.txt", n=1))


def test_parse_hunks_count_and_positions():
    hunks = parse_hunks(_diff())
    assert len(hunks) == 3
    assert hunks[0].old_start == 1 and hunks[0].old_lines == 3
    assert hunks[1].old_start == 6
    assert hunks[1].old == ["line6", "line7", "line8"]
    assert hunks[1].new == ["line6", "SEVEN", "line8"]
    assert hunks[2].old_start == 11 and hunks[2].old_lines == 2


def test_apply_all_equals_new():
    hunks = parse_hunks(_diff())
    out = "\n".join(apply_hunks(OLD.splitlines(), hunks, {0, 1, 2})) + "\n"
    assert out == NEW


def test_apply_none_equals_old():
    hunks = parse_hunks(_diff())
    out = "\n".join(apply_hunks(OLD.splitlines(), hunks, set())) + "\n"
    assert out == OLD


def test_apply_skip_middle_hunk():
    """★ 跳过中间块：后续块的行号仍要对齐（offset 累计的难点）。"""
    hunks = parse_hunks(_diff())
    out_lines = apply_hunks(OLD.splitlines(), hunks, {0, 2})   # 只批准第 1、3 块
    assert out_lines == [
        "line1", "TWO", "line3",                      # 块1 应用
        "line4", "line5", "line6", "line7", "line8",  # 块2 被驳回：原样保留
        "line9", "line10", "line11", "TWELVE"]        # 块3 应用（行号对齐正确）


def test_apply_unified_convenience():
    diff = _diff()
    assert apply_unified(OLD, diff, {0, 1, 2}) == NEW
    assert apply_unified(OLD, diff, set()) == OLD


def test_parse_ignores_headers_and_no_newline_marker():
    diff = _diff() + "\\ No newline at end of file\n"
    assert len(parse_hunks(diff)) == 3


def test_empty_diff_yields_no_hunks():
    assert parse_hunks("") == []
    assert parse_hunks("--- a/x\n+++ b/x\n") == []
    assert apply_hunks(["a"], [], set()) == ["a"]
