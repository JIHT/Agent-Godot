"""permission/rules.py —— yaml 规则 + 分段 glob + 优先级链（M09 §1.1 / §1.5）

门禁规则手册：保安查验证件的顺序是固定的——
① 翻黑名单页（deny 全表：在黑名单里，其他全免谈）
② 查专属授权页（精确工具名 + 精确路径完全相等）
③ 查通用授权页（通配工具名 + 通配路径 glob）
④ 按"默认政策"（defaults[risk]）
顺序错一个，"黑名单的人拿通用卡进门"的事故就来了。

glob 语义（fnmatch 不够，自实现分段匹配）：
- `**` 匹配任意段序列（scripts/** 含 scripts/a/b/c.gd）
- `*` 只匹配单段内字符（scripts/*.gd 不含子目录）
- glob 锚定项目根：绝对路径先归一到根内相对路径，根外路径不匹配任何路径规则
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml

Action = Literal["allow", "ask", "deny"]
Remember = Literal["never", "session", "always"]

# 常见携带文件系统目标的参数键（路径规则的匹配对象）
_PATH_KEYS = ("path", "file", "scene", "root", "dir", "target")

_DEFAULT_DEFAULTS: dict[str, Action] = {"low": "allow", "medium": "ask", "high": "ask"}


@dataclass
class PermissionRule:
    """一条门禁规则：工具（支持通配）× 路径（支持 glob）→ 动作。"""
    tool: str                                    # 支持 "mcp__fetch__*" 通配
    paths: list[str] | None = None               # None = 任意路径（工具级规则）
    action: Action = "ask"
    remember: Remember = "never"                 # "本次会话不再问"的规则记忆

    def matches_tool(self, tool: str) -> bool:
        """工具名匹配：精确相等或 fnmatch 通配（mcp__fetch__*）。"""
        return fnmatch.fnmatchcase(tool, self.tool)

    @property
    def is_tool_wildcard(self) -> bool:
        return any(c in self.tool for c in "*?[")

    @property
    def hash(self) -> str:
        """规则指纹：会话级授权集合（grant_session）的键。"""
        payload = json.dumps(
            {"tool": self.tool, "paths": sorted(self.paths or []),
             "action": self.action},
            ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class Decision:
    """规则引擎的裁决：动作 + 理由 + 命中的规则指纹（审计/会话记忆用）。"""
    action: Action
    reason: str = ""
    matched_rule: str = ""


# ---------- 分段 glob（§1.5：约 15 行的匹配核心） ----------

def _seg_match(pattern_seg: str, seg: str) -> bool:
    """单段匹配：`*` 只在本段内展开，绝不跨越 `/`。"""
    return fnmatch.fnmatchcase(seg, pattern_seg)


def match_glob(pattern: str, path: str) -> bool:
    """分段 glob：`**` 匹配任意段序列，`*` 匹配段内字符。"""
    pat = PurePosixPath(pattern).parts
    p = PurePosixPath(path).parts

    def rec(pi: int, qi: int) -> bool:
        if pi == len(pat):
            return qi == len(p)
        if pat[pi] == "**":
            # `**` 吃掉任意段序列（含零段）
            return any(rec(pi + 1, k) for k in range(qi, len(p) + 1))
        if qi >= len(p):
            return False
        return _seg_match(pat[pi], p[qi]) and rec(pi + 1, qi + 1)

    return rec(0, 0)


def candidate_paths(args: dict) -> list[str]:
    """从工具参数里提取候选路径（path/file/scene/... 及 paths 列表）。"""
    paths: list[str] = []
    for k in _PATH_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v:
            paths.append(v)
    v = args.get("paths")
    if isinstance(v, list):
        paths.extend(str(p) for p in v if p)
    return paths


def normalize_path(p: str, project_root: str | None = None) -> str | None:
    """统一化：反斜杠→posix、`./` 前缀剥掉、绝对路径锚定项目根。

    项目根之外的绝对路径返回 None——锚定意味着 `scripts/**` 不该匹配
    `../other/scripts`（§1.1 易错点②），根外路径不命中任何路径规则。
    """
    s = p.replace("\\", "/")
    if s.startswith("./"):
        s = s[2:]
    if not s.startswith("/"):
        return s                                    # 相对路径按项目根解释
    if project_root:
        root = project_root.replace("\\", "/").rstrip("/")
        if s == root or s.startswith(root + "/"):
            return s[len(root):].lstrip("/") or "."
    return None


def _parse_args(args) -> dict:
    """arguments 可能是 JSON 字符串（协议如此），统一成 dict。"""
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args:
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


class RuleEngine:
    """门禁规则手册的求值器：加载 permissions.yaml，按优先级链裁决。"""

    def __init__(self, config_path: Path | None = None, *,
                 config: dict | None = None,
                 project_root: str | Path | None = None):
        cfg: dict = {}
        if config_path is not None:
            cfg = yaml.safe_load(
                Path(config_path).read_text(encoding="utf-8")) or {}
        elif config is not None:
            cfg = config
        self.project_root = (str(project_root).replace("\\", "/")
                             if project_root else None)
        self.defaults: dict[str, Action] = {
            **_DEFAULT_DEFAULTS, **(cfg.get("defaults") or {})}
        self.rules: list[PermissionRule] = [
            PermissionRule(
                tool=str(r.get("tool", "*")),
                paths=(list(r["paths"]) if r.get("paths") else None),
                action=r.get("action", "ask"),
                remember=r.get("remember", "never"))
            for r in (cfg.get("rules") or [])
        ]
        # "本次会话不再问"：规则指纹集合（snapshot_for_resume 随会话持久化）
        self._session_grants: set[str] = set()

    # ---------- 求值链（§4 步骤 1） ----------

    def decide(self, tool: str, args=None, *,
               risk: str = "medium") -> Decision:
        """①deny 全表 → ②精确规则 → ③通配规则 → ④defaults[risk]，首个命中返回。"""
        a = _parse_args(args)
        paths = [p for p in (normalize_path(x, self.project_root)
                             for x in candidate_paths(a)) if p is not None]

        # ① deny 全表扫（黑名单绝对优先，用户的安全意图不可被通用规则稀释）
        for r in self.rules:
            if r.action == "deny" and r.matches_tool(tool) and self._paths_ok(r, paths):
                return Decision("deny", f"命中 deny 规则: {r.tool}"
                                        + (f" {r.paths}" if r.paths else ""),
                                matched_rule=r.hash)

        # ② 精确工具名规则（含路径精确相等 → 路径 glob 的子顺序）
        if (d := self._first_match(tool, paths, wildcard=False)) is not None:
            return d
        # ③ 通配工具名规则（mcp__fetch__* 这类）
        if (d := self._first_match(tool, paths, wildcard=True)) is not None:
            return d

        # ④ 默认政策（defaults[risk]）
        action = self.defaults.get(risk, "ask")
        return Decision(action, f"默认政策 defaults[{risk}] = {action}")

    def _first_match(self, tool: str, paths: list[str], *,
                     wildcard: bool) -> Decision | None:
        for r in self.rules:
            if r.action == "deny":
                continue                     # deny 已在 ① 扫过
            if r.is_tool_wildcard is not wildcard or not r.matches_tool(tool):
                continue
            if r.paths is None:
                return self._rule_decision(r)          # 工具级规则：路径无关
            if not paths:
                continue                                # 规则限路径但调用没带路径
            if any(p in r.paths for p in paths):        # 精确路径完全相等优先
                return self._rule_decision(r)
        for r in self.rules:
            if r.action == "deny" or r.paths is None:
                continue
            if r.is_tool_wildcard is not wildcard or not r.matches_tool(tool):
                continue
            if not paths:
                continue
            if any(match_glob(pat, p) for pat in r.paths for p in paths):
                return self._rule_decision(r)          # 通配路径兜底
        return None

    def _paths_ok(self, rule: PermissionRule, paths: list[str]) -> bool:
        """deny 规则的路径条件：无 paths 恒真；有 paths 则任一路径命中（精确或 glob）。"""
        if rule.paths is None:
            return True
        if not paths:
            return False
        return (any(p in rule.paths for p in paths)
                or any(match_glob(pat, p) for pat in rule.paths for p in paths))

    def _rule_decision(self, rule: PermissionRule) -> Decision:
        if rule.hash in self._session_grants:
            return Decision("allow", f"会话内已授权（不再问）: {rule.tool}",
                            matched_rule=rule.hash)
        return Decision(rule.action, f"命中规则: {rule.tool}"
                                     + (f" {rule.paths}" if rule.paths else ""),
                        matched_rule=rule.hash)

    # ---------- 会话级授权记忆（§1.1 ③ remember: session） ----------

    def grant_session(self, rule_hash: str) -> None:
        """"本次会话不再问"：规则指纹进会话级 allow 集合。"""
        self._session_grants.add(rule_hash)

    def snapshot_for_resume(self) -> list[str]:
        """会话挂起/恢复时要随身携带的授权指纹（重放恢复不丢"不再问"承诺）。"""
        return sorted(self._session_grants)

    def restore_grants(self, hashes: list[str]) -> None:
        self._session_grants.update(hashes)
