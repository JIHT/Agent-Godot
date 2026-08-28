"""agent/subagents/builtin.py —— 内置角色 ×3（M15 §1.2 ③ / §4 步骤 2）

按角色配模型（§2 问答 4）是 multi 的成本关键杠杆：
    explorer 只读勘察 + 结构化输出 → 廉价档（deepseek-chat）
    verifier 对照验收标准逐条核对 → 廉价档（deepseek-chat）
    coder    真实代码生成 + 调试   → 推理档（deepseek-reasoner）
探查/验收占 multi 工作量的 ~40%，用便宜模型整体省 30%+；全角色旗舰模型
"除了账单没区别"（错误示范）。

角色 = 模板 + 工具表：RoleTemplate 存"与注册表无关的那一半"（提示/模型/预算/
工具筛选条件），materialize(registry) 时才把工具筛选落成**视图**——同一份模板
可以在不同项目的注册表上派生出不同白名单。

自定义角色：把 markdown 角色卡放到 `.agent_godot/agents/<name>.md`
（frontmatter: name/model/tools/steps/tokens…，正文当 role_prompt），
load_custom_specs 会扫进来——与 Claude Code 的 .claude/agents/*.md 同思想。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_godot.tools import ToolRegistry

from .base import DEFAULT_MODEL, Budget, SubagentSpec

REASONING_MODEL = "deepseek/deepseek-reasoner"   # 推理档（coder 专用）


@dataclass
class RoleTemplate:
    """角色模板：与具体注册表无关的角色定义（materialize 时落成 Spec）。"""

    name: str
    role_prompt: str
    description: str = ""
    model: str = DEFAULT_MODEL
    budget: Budget = field(default_factory=Budget)
    tools: tuple[str, ...] = ()          # 工具名或 tag 混写
    readonly: bool | None = None         # True/False = 强制只读/可写；None = 不限

    def materialize(self, registry: ToolRegistry) -> SubagentSpec:
        """模板 + 注册表 → Spec（工具白名单在此落成视图）。"""
        return SubagentSpec(
            name=self.name, role_prompt=self.role_prompt,
            description=self.description, model=self.model,
            budget=self.budget,
            tools=_view(registry, self.tools, self.readonly))


# ---------- 内置三角色（§1.2 ③） ----------

EXPLORER = RoleTemplate(
    name="explorer",
    role_prompt=("你是代码勘察员。只读不写，产出结构化勘察报告：相关文件清单 /"
                 " 关键符号 / 风险点。禁止修改任何文件。"),
    description="只读勘察：摸清相关文件与风险点，为拆解提供事实依据",
    model=DEFAULT_MODEL,
    budget=Budget(steps=8, tokens=20_000, usd=0.05, wall_time=120.0),
    readonly=True)                        # 白名单硬约束（提示里的"禁止"是软约束）

CODER = RoleTemplate(
    name="coder",
    role_prompt=("你是 Godot 实现者。严格按任务书实现，写完必须过 headless 校验，"
                 "只动任务书指定范围内的文件。"),
    description="实现者：按任务书改代码并自检（推理档模型）",
    model=REASONING_MODEL,
    budget=Budget(steps=20, tokens=60_000, usd=0.20, wall_time=300.0),
    tools=("fs", "godot"))               # 文件 + Godot 领域工具（含 headless）

VERIFIER = RoleTemplate(
    name="verifier",
    role_prompt=("你是验收员。逐条核对交付物与验收标准，输出 通过 / 不通过 +"
                 " 问题清单。立场独立，不做修复。"),
    description="验收员：只判不修，产出通过/不通过结论",
    model=DEFAULT_MODEL,
    budget=Budget(steps=6, tokens=15_000, usd=0.05, wall_time=120.0),
    readonly=True)

BUILTIN_ROLES = (EXPLORER, CODER, VERIFIER)


# ---------- 装配 ----------

def build_default_specs(registry: ToolRegistry) -> dict[str, SubagentSpec]:
    """内置三角色 → {name: Spec}（multi 模式的默认工人花名册）。"""
    return {t.name: t.materialize(registry) for t in BUILTIN_ROLES}


def default_agent_roots() -> list[Path]:
    """自定义角色卡目录：项目级 `.agent_godot/agents/`（当前 cwd 下）。"""
    return [Path.cwd() / ".agent_godot" / "agents"]


def load_custom_specs(registry: ToolRegistry,
                      roots: list[Path] | None = None) -> dict[str, SubagentSpec]:
    """扫描 markdown 角色卡（含子目录），重名后来者优先。"""
    out: dict[str, SubagentSpec] = {}
    for root in (roots if roots is not None else default_agent_roots()):
        root = Path(root)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                spec = SubagentSpec.from_markdown(path, registry)
            except ValueError as e:      # 坏卡片跳过（角色卡是数据不是代码）
                continue
            out[spec.name] = spec
    return out


def build_all_specs(registry: ToolRegistry,
                    roots: list[Path] | None = None) -> dict[str, SubagentSpec]:
    """内置 + 自定义（自定义可覆盖内置同名角色——用户说了算）。"""
    specs = build_default_specs(registry)
    specs.update(load_custom_specs(registry, roots))
    return specs


def describe_specs(specs: dict[str, SubagentSpec]) -> str:
    """`/agents list` 的人读视图：角色 / 模型 / 预算 / 工具数。"""
    if not specs:
        return "（没有可用子代理角色）"
    lines = [f"共 {len(specs)} 个子代理角色："]
    for name, spec in sorted(specs.items()):
        kind = "A2A远程" if spec.is_remote else "本地"
        tools = spec.tools.names()
        shown = ", ".join(tools[:6]) + ("…" if len(tools) > 6 else "")
        lines.append(
            f"  {name:<12} [{kind}] {spec.model}\n"
            f"      {spec.description or spec.role_prompt[:40]}\n"
            f"      预算 steps={spec.budget.steps} tokens={spec.budget.tokens}"
            f" wall={spec.budget.wall_time:.0f}s\n"
            f"      工具({len(tools)}): {shown or '（无）'}")
    return "\n".join(lines)


def _view(registry: ToolRegistry, tools: tuple[str, ...],
          readonly: bool | None) -> ToolRegistry:
    """工具筛选：空 = 全量（再按 readonly 过滤），非空 = 工具名/tag 混写白名单。"""
    from .base import tools_view
    return tools_view(registry, list(tools), readonly)


__all__ = ["BUILTIN_ROLES", "CODER", "EXPLORER", "VERIFIER", "REASONING_MODEL",
           "RoleTemplate", "build_all_specs", "build_default_specs",
           "default_agent_roots", "describe_specs", "load_custom_specs"]
