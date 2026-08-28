"""skills/loader.py —— SKILL.md 的扫描 / 解析 / 按需注入（M14 §1.3）

书架上的专业手册：常驻上下文只有**目录卡**（每技能一行 ≈15 token），
干活遇到"要打包了"才取下手册翻开（全文注入）。对比记忆（M08）：
    记忆存"发生过的事实"（会过时可遗忘） = 员工的工作日志
    Skills 存"怎么做某类事的方法论"（稳定、版本化） = 公司的 SOP 文件柜

成本账（§7 问答 7）：20 个技能全文常驻 ≈16k token（每次对话开场就烧掉
预算的 1/8，且 Lost in the Middle 把真话挤到低注意力区）；按需加载把成本
从"每对话 16k"降到"用到的对话 +800"——两个数量级。

双轨触发（§1.3 ①）：①目录里的触发词提示 ②模型主动调 skill_use。
触发词只是**提示**，最终由模型判断要不要用——把模式匹配外包给理解能力的
持有者（与 M12 意图分类选 LLM 同一逻辑）。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent_godot.tools import ToolResponse  # noqa: F401 —— 类型提示/文档引用

SKILL_FILE = "SKILL.md"

# frontmatter：--- 开头，--- 结尾（允许 CRLF）
_FRONT_RE = re.compile(r"^---[ \t]*\r?\n(?P<fm>.*?)\r?\n---[ \t]*(?:\r?\n|$)",
                       re.S)
_HEADING_RE = re.compile(r"^#\s+(?P<title>.+)$", re.M)


def _scalar(raw: str) -> str:
    """去引号与行注释（frontmatter 标量值）。"""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(v) for v in value if str(v).strip()]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """拆出 (frontmatter 字段, 正文)。没有 frontmatter 时字段为空 dict。

    手写解析而不用 yaml：①少一个依赖 ②技能头就四个字段，出错面小
    ③解析失败要能"降级成可用技能"而不是抛异常（技能文档是数据不是代码）。
    """
    m = _FRONT_RE.match(text)
    if m is None:
        return {}, text
    data: dict = {}
    pending_key: str | None = None
    for line in m.group("fm").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and pending_key:
            data.setdefault(pending_key, []).append(_scalar(stripped[2:]))
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key, value = key.strip(), value.strip()
        if value == "":                       # 可能是块列表的开始
            data[key] = []
            pending_key = key
        elif value.startswith("[") and value.endswith("]"):
            data[key] = [_scalar(x) for x in value[1:-1].split(",") if x.strip()]
            pending_key = key
        else:
            data[key] = _scalar(value)
            pending_key = None
    return data, text[m.end():]


@dataclass
class Skill:
    """一本手册：元数据（常在目录里）+ 正文（按需才读）。"""

    name: str
    triggers: list[str] = field(default_factory=list)
    tools_needed: list[str] = field(default_factory=list)
    version: int = 1
    body: str = ""
    source: Path = Path(".")
    description: str = ""

    @classmethod
    def from_markdown(cls, path: Path) -> "Skill":
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return cls(name=path.parent.name or path.stem, source=path)
        data, body = parse_frontmatter(raw)
        body = body.strip()
        name = str(data.get("name") or "").strip() or path.parent.name or path.stem
        try:
            version = int(str(data.get("version", 1)).strip())
        except ValueError:
            version = 1
        heading = _HEADING_RE.search(body)
        description = str(data.get("description") or "").strip()
        if not description and heading:
            description = heading.group("title").strip()
        return cls(name=name, triggers=_as_list(data.get("triggers")),
                   tools_needed=_as_list(data.get("tools_needed")),
                   version=version, body=body, source=path,
                   description=description)

    def matches(self, text: str) -> bool:
        """触发词命中判定（大小写不敏感；名字本身也算一个触发词）。"""
        if not text:
            return False
        low = text.lower()
        return any(t.lower() in low for t in (*self.triggers, self.name))

    def catalog_line(self, max_triggers: int = 3) -> str:
        trig = "/".join(self.triggers[:max_triggers])
        desc = f": {self.description}" if self.description else ""
        suffix = f"（v{self.version}）"
        return f"- {self.name}[{trig}]{desc}{suffix}" if trig else \
               f"- {self.name}{desc}{suffix}"


class SkillLoader:
    """技能目录的扫描与按需加载（无状态 IO：scan 一次，load 多次）。"""

    def __init__(self, roots: list[Path] | None = None, counter=None):
        self.roots: list[Path] = list(roots) if roots else default_roots()
        self._counter = counter
        self._skills: list[Skill] = []
        self._by_name: dict[str, Skill] = {}
        self.loaded: list[str] = []          # 本会话已加载的技能（trace/记账）

    # ---------- 扫描 ----------

    def scan(self, roots: list[Path] | None = None) -> list[Skill]:
        """扫描全部 root 下的 SKILL.md，重名取高版本（同版本后来者优先）。"""
        if roots is not None:
            self.roots = list(roots)
        found: dict[str, Skill] = {}
        for root in self.roots:
            root = Path(root)
            if not root.exists():
                continue
            for path in sorted(root.rglob(SKILL_FILE)):
                skill = Skill.from_markdown(path)
                if not skill.name:
                    continue
                old = found.get(skill.name)
                if old is None or skill.version >= old.version:
                    found[skill.name] = skill      # 高版本 / 后扫到的覆盖
        self._skills = sorted(found.values(), key=lambda s: s.name)
        self._by_name = {s.name: s for s in self._skills}
        return self._skills

    def add_root(self, root: Path) -> "SkillLoader":
        if Path(root) not in self.roots:
            self.roots.append(Path(root))
        return self

    # ---------- 查询 ----------

    @property
    def skills(self) -> list[Skill]:
        return self._skills

    def names(self) -> list[str]:
        return [s.name for s in self._skills]

    def by_name(self, name: str) -> Skill:
        if not self._by_name:
            self.scan()
        if name not in self._by_name:
            near = self.nearest(self.names(), name)
            hint = f"（近邻: {near[0]}）" if near else ""
            raise KeyError(f"没有技能 {name!r}{hint}")
        return self._by_name[name]

    def match(self, text: str) -> list[Skill]:
        """触发词命中的技能（空输入返回空——别把整本目录当命中）。"""
        return [s for s in self._skills if s.matches(text)]

    # ---------- 目录与加载 ----------

    def catalog_prompt(self, max_triggers: int = 3) -> str:
        """常驻目录：每技能一行（§1.3 ③）。没有技能时返回空串。"""
        if not self._skills:
            return ""
        lines = ["可用技能（需要时用 skill_use 工具取全文，不要凭记忆操作）:"]
        lines.extend(s.catalog_line(max_triggers) for s in self._skills)
        return "\n".join(lines)

    async def load(self, name: str) -> str:
        """按需取全文：包成 <skill> 标签注入本轮上下文（§1.3 ③）。"""
        skill = self.by_name(name)
        self.loaded.append(skill.name)
        return (f"<skill name='{skill.name}' version='{skill.version}'>\n"
                f"{skill.body}\n</skill>")

    # ---------- 记账 ----------

    def _counter_or_new(self):
        if self._counter is None:
            from agent_godot.context.token_counter import TokenCounter
            self._counter = TokenCounter()
        return self._counter

    def estimate_text(self, text: str) -> int:
        return self._counter_or_new().estimate_text(text)

    def catalog_tokens(self) -> int:
        """目录常驻成本（验收：20 个技能 < 300 token）。"""
        return self.estimate_text(self.catalog_prompt())

    def body_tokens(self, name: str) -> int:
        return self.estimate_text(self.by_name(name).body)

    @staticmethod
    def nearest(names: list[str], query: str, n: int = 3) -> list[str]:
        """技能名近邻提示（/skills use 打包 → 打包发布）。"""
        return difflib.get_close_matches(query, names, n=n, cutoff=0.5)


def default_roots() -> list[Path]:
    """内置技能 + 项目技能（项目目录里放 .agent_godot/skills/<名>/SKILL.md 即生效）。"""
    return [Path(__file__).parent / "builtin",
            Path.cwd() / ".agent_godot" / "skills"]


__all__ = ["SKILL_FILE", "Skill", "SkillLoader", "default_roots",
           "parse_frontmatter"]
