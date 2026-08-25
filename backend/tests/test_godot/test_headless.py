"""tests/test_godot/test_headless.py —— headless 校验器（M06 §1.5）。

不起真 Godot（CI 不保证装了引擎）：只测错误解析器与二进制定位；
真机四级校验留给验收 Demo。
"""
from pathlib import Path

from agent_godot.tools.godot.headless import (GD_ERROR, GodotRunner,
                                              find_godot)


def test_gd_error_regex_basic():
    out = ("res://bad.gd:5 - Parse Error: Unexpected token.\n"
           "res://ok.gd:1 this line is fine\n")
    errs = [m.groupdict() for m in GD_ERROR.finditer(out)]
    assert len(errs) == 1                           # 非 error 行不误报
    assert errs[0]["file"] == "res://bad.gd"
    assert errs[0]["line"] == "5"
    assert "Parse Error" in errs[0]["msg"]


def test_gd_error_regex_range_and_script_error():
    out = "res://a.gd:10-12 - Script Error: Cannot find member \"foo\" in self."
    errs = [m.groupdict() for m in GD_ERROR.finditer(out)]
    assert len(errs) == 1
    assert errs[0]["end"] == "12"
    assert "Script Error" in errs[0]["msg"]


def test_parse_errors_via_runner():
    out = "SCRIPT ERROR: bad stuff\n  at: _ready (res://x.gd:3)\nres://x.gd:3 - Parse Error: oops error\n"
    errs = GodotRunner.parse_errors(out)
    assert len(errs) == 1
    assert errs[0]["line"] == "3"


def test_find_godot_from_env(tmp_path: Path, monkeypatch):
    fake = tmp_path / "godot.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("GODOT_BIN", str(fake))
    assert find_godot() == str(fake)


def test_find_godot_missing(monkeypatch):
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr("agent_godot.tools.godot.headless.shutil.which",
                        lambda _c: None)
    assert find_godot() is None


def test_res_path_normalization(tmp_path: Path):
    runner = GodotRunner("whatever", tmp_path)
    assert runner._res("player.gd") == "res://player.gd"
    assert runner._res("scenes\\main.tscn") == "res://scenes/main.tscn"
    assert runner._res("res://a.gd") == "res://a.gd"
