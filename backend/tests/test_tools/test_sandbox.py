"""tests/test_tools/test_sandbox.py —— 沙箱三道闸的单测（M04 §5）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_godot.tools.sandbox import (DeniedPathError, PathEscapeError,
                                       resolve_in_root, truncate)


def test_path_traversal_blocked(tmp_path: Path):
    """path traversal 被拦截：../ 越出项目根 → PathEscapeError。"""
    (tmp_path / "scripts").mkdir()
    with pytest.raises(PathEscapeError):
        resolve_in_root(tmp_path, "scripts/../../secrets.key")


def test_denylist_blocked(tmp_path: Path):
    """敏感目录黑名单：.git/config → DeniedPathError。"""
    with pytest.raises(DeniedPathError):
        resolve_in_root(tmp_path, ".git/config")


def test_denylist_env_blocked(tmp_path: Path):
    """.env 也是黑名单成员（密钥文件）。"""
    with pytest.raises(DeniedPathError):
        resolve_in_root(tmp_path, "backend/.env")


def test_legal_path_passes(tmp_path: Path):
    """合法相对路径正常解析并返回根内绝对路径。"""
    p = resolve_in_root(tmp_path, "lab/m01/attention.py")
    assert p.is_relative_to(tmp_path.resolve())


def test_windows_backslash_normalized(tmp_path: Path):
    """Windows 反斜杠输入与正斜杠等价（posix 化后再判定）。"""
    p1 = resolve_in_root(tmp_path, "a/b/c.txt")
    p2 = resolve_in_root(tmp_path, "a\\b\\c.txt")
    assert p1 == p2


def test_truncate_head_tail():
    """截断保头保尾：头 1500 + 尾 300 + 中间省略标记。"""
    text = "A" * 5000
    out = truncate(text)
    assert out.startswith("A" * 1500)
    assert out.endswith("A" * 300)
    assert "中间省略" in out
    assert len(out) < 2000


def test_truncate_short_text_untouched():
    assert truncate("hello") == "hello"
