"""tests/test_context/test_truncator.py —— M07 §5：L1 截断 / L2 结构化摘要。"""
from __future__ import annotations

import json

from agent_godot.context import ObservationTruncator


def test_l1_truncate_keeps_head_and_tail():
    t = ObservationTruncator()
    long_text = "HEAD " * 500 + "MIDDLE " * 500 + "TAIL " * 100
    out = t.truncate(long_text, budget=2000)
    assert len(out) < len(long_text)
    assert "HEAD" in out and "TAIL" in out       # 头尾都保住
    assert "省略" in out                          # 有省略标记


def test_l1_short_content_passthrough():
    t = ObservationTruncator()
    assert t.truncate("short", budget=2000) == "short"
    assert t.truncate(None) == ""


def test_l1_never_cuts_json_in_half():
    """JSON 截成两半比超长更糟——合法超长 JSON 走结构化摘要。"""
    t = ObservationTruncator()
    fat_json = json.dumps([{"file": f"scene_{i}.tscn", "size": 1234}
                           for i in range(5000)])
    out = t.truncate(fat_json, budget=2000)
    assert len(out) < 2000
    assert "共 5000 项" in out                    # 结构摘要而非残缺 JSON
    assert "前 20 项" in out


def test_l2_directory_listing_summary():
    """100KB 目录列表 → 2k 内且含"共 N 行"结构摘要。"""
    t = ObservationTruncator()
    listing = "\n".join(f"{i:04d}  script_{i}.gd" for i in range(20_000))
    out = t.summarize_struct("list_dir", listing)
    assert len(out) <= 2000
    assert "共 20000 行" in out
    assert "list_dir" in out
    assert "script_0" in out                     # 骨架保留


def test_l2_json_list_summary():
    t = ObservationTruncator()
    raw = json.dumps([f"file_{i}" for i in range(300)])
    out = t.summarize_struct("search", raw)
    assert "共 300 项" in out and "前 20 项" in out


def test_l2_json_object_skeleton():
    t = ObservationTruncator()
    raw = json.dumps({f"key_{i}": i for i in range(50)})
    out = t.summarize_struct("read_json", raw)
    assert "顶层键 50 个" in out and "key_0" in out


def test_l2_log_error_extraction():
    t = ObservationTruncator()
    log = "\n".join(
        f"[INFO] step {i}" if i % 10 else f"[ERROR] crash at {i}"
        for i in range(100))
    out = t.summarize_struct("run_headless", log)
    assert "错误/警告" in out and "ERROR" in out
    assert "INFO" not in out                      # 只保留错误行骨架
