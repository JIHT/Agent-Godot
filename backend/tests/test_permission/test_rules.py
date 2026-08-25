"""tests/test_permission/test_rules.py —— M09 §1.5 / §5：规则引擎表驱动测试。

固定 `**` 与 `*` 的语义表（将来最容易被用户报 bug 的地方）+ deny 优先级
+ 精确路径优先 + 会话级授权记忆 + yaml 加载。
"""
from __future__ import annotations

from pathlib import Path

from agent_godot.permission.rules import (RuleEngine, match_glob,
                                          normalize_path)

# ---------- 分段 glob 语义表（10 用例起步） ----------

GLOB_CASES = [
    # (pattern, path, expected)
    ("scripts/**", "scripts/a.gd", True),            # ** 含一层
    ("scripts/**", "scripts/a/b/c.gd", True),        # ** 含任意深度
    ("scripts/**", "scripts", True),                 # ** 可吃零段
    ("scripts/*.gd", "scripts/a.gd", True),          # * 单段内
    ("scripts/*.gd", "scripts/a/b.gd", False),       # * 不跨段（fnmatch 会误配！）
    ("**/*.gd", "a.gd", True),                       # **/*.gd 含根层
    ("**/*.gd", "x/y/z.gd", True),
    ("*.gd", "a/b.gd", False),                       # *.gd 只匹配当前层
    ("scenes/*/player.tscn", "scenes/a/player.tscn", True),
    ("scenes/*/player.tscn", "scenes/a/b/player.tscn", False),
    ("**", "anything/at/all.txt", True),             # 裸 ** 匹配一切
]


def test_glob_semantics_table():
    for pattern, path, expected in GLOB_CASES:
        assert match_glob(pattern, path) is expected, (pattern, path)


def test_normalize_path_windows_and_anchor():
    # 反斜杠统一 posix
    assert normalize_path("scripts\\a.gd") == "scripts/a.gd"
    # ./ 前缀剥掉
    assert normalize_path("./scripts/a.gd") == "scripts/a.gd"
    # 绝对路径锚定项目根
    assert normalize_path("/proj/scripts/a.gd", "/proj") == "scripts/a.gd"
    # 根外路径不匹配任何路径规则（glob 锚定项目根）
    assert normalize_path("/other/scripts/a.gd", "/proj") is None


def test_deny_rule_never_overridden():
    """deny 最高优先：allow 规则（无论位置）盖不住 deny（§1.1 易错点①）。"""
    engine = RuleEngine(config={
        "rules": [
            {"tool": "*", "action": "allow"},                    # 全放行在前
            {"tool": "delete_file", "action": "deny"},           # 黑名单在后
        ],
        "defaults": {"high": "ask"}})
    assert engine.decide("delete_file", {}).action == "deny"
    # 通配 deny：任何 fetch 工具都被拒
    engine2 = RuleEngine(config={"rules": [
        {"tool": "mcp__fetch__*", "action": "deny"}]})
    assert engine2.decide("mcp__fetch__get_url", {}).action == "deny"
    assert engine2.decide("mcp__other__x", {}).action == "ask"   # 不相关走默认


def test_exact_path_beats_wildcard_rule():
    """精确路径规则优先于通配规则（专属授权页在通用授权页之前）。"""
    engine = RuleEngine(config={"rules": [
        {"tool": "write_file", "paths": ["scripts/**"], "action": "allow"},
        {"tool": "write_file", "paths": ["scripts/main.gd"], "action": "ask"},
    ]})
    assert engine.decide(
        "write_file", {"path": "scripts/main.gd"}).action == "ask"   # 精确命中
    assert engine.decide(
        "write_file", {"path": "scripts/other.gd"}).action == "allow"  # 通配兜底


def test_defaults_by_risk():
    engine = RuleEngine(config={})
    assert engine.decide("echo", {}, risk="low").action == "allow"
    assert engine.decide("mark", {}, risk="medium").action == "ask"
    assert engine.decide("run", {}, risk="high").action == "ask"


def test_rule_without_paths_matches_any_path():
    """无 paths 的工具级规则：路径无关命中。"""
    engine = RuleEngine(config={"rules": [
        {"tool": "godot_run_scene", "action": "ask", "remember": "session"}]})
    assert engine.decide("godot_run_scene", {"scene": "res://x.tscn"}).action == "ask"
    assert engine.decide("godot_run_scene", {}).action == "ask"


def test_path_rule_requires_path_in_args():
    """规则限路径但调用没带路径 → 规则不命中，落 defaults。"""
    engine = RuleEngine(config={"rules": [
        {"tool": "write_file", "paths": ["scripts/**"], "action": "allow"}]})
    assert engine.decide("write_file", {}, risk="medium").action == "ask"


def test_grant_session_remember():
    """"本次会话不再问"：规则指纹进会话级 allow 集合，快照可随会话持久化。"""
    engine = RuleEngine(config={"rules": [
        {"tool": "godot_run_scene", "action": "ask", "remember": "session"}]})
    d1 = engine.decide("godot_run_scene", {"scene": "res://a.tscn"})
    assert d1.action == "ask"
    engine.grant_session(d1.matched_rule)
    d2 = engine.decide("godot_run_scene", {"scene": "res://a.tscn"})
    assert d2.action == "allow"
    assert "已授权" in d2.reason
    snapshot = engine.snapshot_for_resume()
    assert d1.matched_rule in snapshot
    # 恢复：新引擎重放快照后不再问
    engine3 = RuleEngine(config={"rules": [
        {"tool": "godot_run_scene", "action": "ask", "remember": "session"}]})
    engine3.restore_grants(snapshot)
    assert engine3.decide("godot_run_scene", {}).action == "allow"


def test_load_from_yaml(tmp_path: Path):
    """permissions.yaml 产品配置面能被正确加载。"""
    p = tmp_path / "permissions.yaml"
    p.write_text("""
defaults:
  low: allow
  medium: ask
  high: ask
rules:
  - tool: write_file
    paths: ["scripts/**"]
    action: allow
  - tool: delete_file
    action: deny
""", encoding="utf-8")
    engine = RuleEngine(p, project_root=str(tmp_path))
    assert engine.decide("write_file", {"path": "scripts/a.gd"}).action == "allow"
    assert engine.decide("delete_file", {}).action == "deny"


def test_absolute_path_anchored_to_project_root():
    """glob 锚定项目根：根外绝对路径不命中路径规则（防 ../other/scripts 逃逸）。"""
    engine = RuleEngine(
        config={"rules": [
            {"tool": "write_file", "paths": ["scripts/**"], "action": "allow"}]},
        project_root="/proj")
    assert engine.decide(
        "write_file", {"path": "/proj/scripts/a.gd"}, risk="medium").action == "allow"
    assert engine.decide(
        "write_file", {"path": "/other/scripts/a.gd"}, risk="medium").action == "ask"
