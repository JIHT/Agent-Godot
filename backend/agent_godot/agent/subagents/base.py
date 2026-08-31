"""agent/subagents/base.py —— 子代理定义与派生（M15 §1.2 / §4 步骤 1）

临时工合同制（§1.2 ②）：开工前签合同（Spec：岗位职责/可用工具/用哪个档位的
模型/预算上限），干完活交**一页交付报告**，合同终止——临时工的草稿纸（过程
上下文）不搬进包工头办公室（主控上下文），只带走报告。

★ 隔离的命门在 spawn 的最后一行（§2 问答 2）：Session 是局部变量，函数返回
即随子代理销毁；回传给主控的只有 SubtaskResult（report/artifacts/usage）。
若把过程消息也回传：①主控上下文被 N 份过程数据淹没（隔离失效，回到单 Agent
滚雪球）；②子代理间的中间态互相污染（explorer 的犹豫笔记影响 coder 判断）。

★ 工具白名单是"视图不是拷贝"：registry.filter 返回新 ToolRegistry，里面装的
是**同一批工具实例**（共享乐观锁状态），所以"物理上给不出写工具"与"写文件
仍受乐观锁保护"两者兼得（M04 §1.2）。

★ 子代理**不挂确认门**（M09 权限门）：无人值守执行，约束来自白名单（硬）+ 角色
提示里的禁止项（软）+ 独立预算（兜底）。预算耗尽即"自杀式"返回报告，不向主控
求救——否则隔离失效（§1.2 易错点②）。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from agent_godot.core import Message, Usage
from agent_godot.tools import ToolRegistry

from ..dispatcher import Dispatcher
from ..loop import AgentLoop, LoopConfig, Session
from ..paradigms.base import ModeStrategy

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "deepseek/deepseek-chat"      # 廉价档（探查/验收）

# 任务书里出现的路径字段（用于从工具调用里回收"产出物清单"，聚合时查冲突）
_PATH_KEYS = ("path", "file", "target", "filename", "script", "scene")


# ---------- ① 预算（子代理独立于主控，防爆仓） ----------

@dataclass
class Budget:
    """子代理四维预算（M03 BudgetTracker 的配方，不是记账器本身）。

    独立于主控：子代理超预算**不向主控求救**（那等于把子任务的困境塞回主控
    上下文，隔离失效），而是优雅收尾（M03 _graceful_wrap_up）后返回报告。
    """

    steps: int = 12
    tokens: int = 40_000
    usd: float = 0.20
    wall_time: float = 240.0

    def to_loop_config(self) -> LoopConfig:
        return LoopConfig(max_steps=self.steps, token_budget=self.tokens,
                          usd_budget=self.usd,
                          wall_time_budget=self.wall_time)


# ---------- ② 交付报告（唯一回传物） ----------

@dataclass
class SubtaskResult:
    """子代理交回主控的**全部**东西（§1.2 ③：报告是蒸馏产物）。

    title / attempts 由 Orchestrator 回填（spawn 本身不关心编排层信息）。

    ★ 两个后加字段都服务于"跨子任务"的链路，而不是单个子代理自己：
      - `assumptions`（§1.6）：交付报告第 5 条抽出来的**自报假设**。聚合层拿它
        与项目硬约定求交，捕获"主控自己都不知道自己漏写了约定"的静默补全。
      - `file_hashes`（§1.4 ⑤-4）：改动文件的内容指纹。串行链上后继要拿到前驱
        的基线 hash，才能"增量修改而非重写"，且让乐观锁一次命中。
    """

    spec_name: str
    ok: bool
    report: str                                 # 交付报告（唯一回传物）
    artifacts: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=lambda: Usage(0, 0))
    stop_reason: str = "natural"
    title: str = ""
    attempts: int = 1
    assumptions: list[str] = field(default_factory=list)
    file_hashes: dict[str, str] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.usage.input_tokens + self.usage.output_tokens

    def summary(self, limit: int = 200) -> str:
        """聚合视图里的一行摘要（报告截断，防止聚合报告本身爆炸）。

        截到第 5 条（自报假设）之前：假设另有一栏单独渲染，塞进摘要里会把
        "结论"挤掉——聚合报告是给人看的，一屏要能看到每个子任务的结论。
        """
        text = " ".join((self.report or "").split())
        cut = re.search(r"\s5\s*[.、:：)]", text)
        if cut:
            text = text[:cut.start()].rstrip() + " …"
        if len(text) > limit:
            text = text[:limit] + "…"
        flag = "通过" if self.ok else f"未完成({self.stop_reason})"
        return f"[{flag}] {self.title or self.spec_name}: {text or '（无报告）'}"


# ---------- ③ 角色合同 ----------

@dataclass
class SubagentSpec:
    """子代理合同四要素（§1.2 ①）：角色提示 + 工具视图 + 模型档位 + 预算。

    remote：A2A 远程执行器（M15 §1.5）。非 None 时 spawn 走 HTTP 而非本地
    Loop——同一个 SubtaskResult 出口，编排层分不出本地工人和外包工人（Adapter）。
    """

    name: str
    role_prompt: str
    tools: ToolRegistry
    model: str = DEFAULT_MODEL
    budget: Budget = field(default_factory=Budget)
    description: str = ""
    remote: Callable[[str, dict], Awaitable[SubtaskResult]] | None = None

    @property
    def is_remote(self) -> bool:
        return self.remote is not None

    @classmethod
    def from_markdown(cls, path: Path | str, registry: ToolRegistry) -> "SubagentSpec":
        """从 markdown 角色卡构造 Spec（`.claude/agents/*.md` 同思想）。

        frontmatter 字段（全部可选，缺失用内置默认）：
            name / description / model / tools（工具名或 tag 混写） /
            readonly（true=只要只读工具）/ steps / tokens / usd / wall_time
        正文（frontmatter 之后）整体作为 role_prompt。
        """
        from agent_godot.skills.loader import parse_frontmatter  # 复用 M14 解析器

        p = Path(path)
        try:
            raw = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ValueError(f"读不到角色卡 {p}: {e}") from e
        data, body = parse_frontmatter(raw)
        name = str(data.get("name") or "").strip() or p.stem
        readonly = _as_bool(data.get("readonly"))
        return cls(
            name=name,
            role_prompt=(body or "").strip(),
            tools=tools_view(registry, _as_list(data.get("tools")), readonly),
            model=str(data.get("model") or DEFAULT_MODEL).strip(),
            budget=Budget(steps=_as_int(data.get("steps"), 12),
                          tokens=_as_int(data.get("tokens"), 40_000),
                          usd=_as_float(data.get("usd"), 0.20),
                          wall_time=_as_float(data.get("wall_time"), 240.0)),
            description=str(data.get("description") or "").strip())


# ---------- ④ 子代理策略（工具视图 = 白名单，不再按模式裁剪） ----------

class WhitelistStrategy(ModeStrategy):
    """子代理契约：工具集**就是** Spec 白名单，Loop 不要再裁一刀。

    故意**不注册**进 PARADIGMS（它不是模式，M13 的四模式集合不能被污染）；
    由 spawn 直接实例化传给 loop.run(strategy=...)。

    temperature 取 0.2：子代理干活要稳（比 ask 的 0.7 低，比 craft 的 0.1 略高
    ——它的产出是报告，允许一点表达自由度）。
    """

    mode = "subagent"

    def __init__(self, temperature: float = 0.2):
        super().__init__()
        self.config.temperature = temperature


# ---------- ⑤ 派生：一次性派活-收工-销毁 ----------

async def spawn(spec: SubagentSpec, task: str,
                session_ctx: dict | None = None) -> SubtaskResult:
    """派生一个子代理跑一次性任务书，返回交付报告（上下文不回传）。

    session_ctx（可选，主控注入的"共享事实"）：
        llm         ：直接指定 LLM 实例（测试 / 主控想让子代理用同一连接）
        model       ：配合 llm 的模型名（记账与 system 用）
        llm_factory ：Callable[[model_ref], LLM]（按角色配模型的生产用法）
        bus         ：EventBus（子代理事件并进主控事件流，trace 才看得见）
        digest      ：项目现状摘要（M15 §3：任务书自包含的补漏通道）

    生命周期：新 Session → 白名单 Dispatcher → AgentLoop 跑一次 → 取报告销毁。
    """
    ctx = dict(session_ctx or {})
    if spec.remote is not None:                       # A2A 远程工人（适配器）
        return await spec.remote(task, ctx)

    llm, model = _resolve_llm(spec, ctx)
    bus = ctx.get("bus")
    session = Session(session_id=f"sub-{spec.name}-{uuid.uuid4().hex[:8]}")
    # 白名单视图进 Dispatcher：模型看不见也调不到白名单外的工具（物理边界）
    dispatcher = Dispatcher(spec.tools, session=session)
    loop = AgentLoop(llm, dispatcher, model=model, bus=bus,
                     config=spec.budget.to_loop_config())

    if bus is not None:
        await bus.emit("subagent_start", spec=spec.name, model=model,
                       tools=spec.tools.names())
    result = await loop.run(session, _task_prompt(spec, task, ctx),
                            strategy=WhitelistStrategy())
    report = result.final_text or ""
    out = SubtaskResult(
        spec_name=spec.name,
        ok=result.stop_reason == "natural",
        report=report,
        artifacts=_extract_artifacts(session, spec.tools),
        usage=result.usage_total or Usage(0, 0),
        stop_reason=result.stop_reason,
        # ★ 假设必须**结构化**回填，不能让聚合层去解析自由文本
        #   （§1.6 ⑥-2：格式一变就全漏，与 M03 的 Observation 同一原则）
        assumptions=_extract_assumptions(report))
    if bus is not None:
        await bus.emit("subagent_done", spec=spec.name, ok=out.ok,
                       stop_reason=out.stop_reason, tokens=out.tokens)
    # ★ session 在此随栈帧销毁——过程上下文不回传（隔离的命门）
    return out


# 交付要求（§1.6 ③(c)：第 5 条"自报假设"是一行提示换一个显式信号）
DELIVERY_SPEC = (
    "完成后直接输出交付报告，结构：\n"
    "1. 结论（完成 / 部分完成 / 无法完成 + 一句话理由）\n"
    "2. 产出清单（新增/修改的文件路径，没有就写「无」）\n"
    "3. 关键决策（技术选型与理由，1~3 条）\n"
    "4. 遗留问题与风险\n"
    "5. **你的假设**：列出本次你做的技术决策中，**任务书未明确要求、由你自己**"
    "**判断**的部分（格式 / 路径 / 命名 / 依赖选型等）。每条一行，没有就写「无」。\n"
    "   ★ 不要写套话——这一条的作用是让主控发现「你脑补了任务书没说的东西」。\n"
    "   ★ 反例：不要写「项目使用 Godot」这类任务书**已经隐含**的内容，\n"
    "     只写任务书**没说而你自己定了**的（例：「任务书未指定序列化格式，\n"
    "     我按社区常见做法选用了 JSON」）。\n"
    "报告控制在 800 字内——只回传结论，过程留在你自己这里。")


def _task_prompt(spec: SubagentSpec, task: str, ctx: dict) -> str:
    """任务书 = 角色 + 任务 + （项目现状摘要）+ CONSTRAINTS + 交付格式要求。

    §3 难点：任务书必须**自包含**——主控"当然知道"的背景（项目约定/之前的
    对话）子代理一无所知（隔离是双向的）。digest 是补漏通道，交付要求段
    强制报告结构化（否则聚合层拿到一堆自由文本没法做一致性检查）。

    ★ CONSTRAINTS 与 digest 必须**分成两段**，且**无条件**注入（§1.6 ③(b)）：
      - digest     ：软信息（最近 N 条对话摘要），随对话滚动漂移，有才注入；
      - CONSTRAINTS：硬约束（不可协商的项目约定），稳定、无条件注入，空也要
        注入空标记。混成一段会被模型当成"参考背景"而非"硬要求"，软约束挡
        不住脑补（§1.6 ⑤-2）。
      - 注入是**全局的**（不给任何角色开后门）：verifier 也拿同一份，验收标准
        才不等于 brief 本身——否则 brief 漏的验收也漏（同源一起漏，§1.6 ⑤-1）。
    """
    parts = [f"# 角色\n{spec.role_prompt}", f"# 任务书\n{task}"]
    if digest := str(ctx.get("digest") or "").strip():
        parts.append("# 项目现状摘要（参考背景）\n" + digest)
    constraints = ctx.get("constraints")
    if constraints is None:                     # 没传就自己按项目根加载（防御）
        constraints = load_constraints(ctx.get("project_root"))
    parts.append("# 项目硬性约定（不可协商，违反即验收不通过）\n"
                 + _constraints_text(constraints))
    # 串行链上下文（§1.4 ⑤-4）：前驱改了什么 + 当前基线 hash，防语义覆盖
    if chain := str(ctx.get("chain_hint") or "").strip():
        parts.append(chain)
    parts.append("# 交付要求\n" + DELIVERY_SPEC)
    return "\n\n".join(parts)


def _constraints_text(constraints) -> str:
    """CONSTRAINTS 块的正文（空也要给显式标记——让模型知道"确实没有"）。"""
    if constraints is None:
        return "（本项目暂无登记约定）"
    text = getattr(constraints, "text", constraints)
    return str(text or "").strip() or "（本项目暂无登记约定）"


def _extract_assumptions(report: str) -> list[str]:
    """从交付报告的第 5 条里抽出**结构化**的假设清单（§1.6 ⑥-2）。

    只认编号 5 那一段（到下一段编号或空两行为止），逐行去 bullet 前缀；
    「无 / （无）/ None」当空清单——模型按格式写"无"是正常的。
    """
    if not report:
        return []
    lines = str(report).splitlines()
    start = -1
    inline = ""
    for i, line in enumerate(lines):
        s = line.strip().lstrip("#>*").strip()
        m = re.match(r"^5\s*[.、:：)]\s*(.*)$", s)
        if m:
            start, inline = i, m.group(1).strip()
            break
    if start < 0:
        return []
    out: list[str] = []
    # 剥掉行内小标题（"5. 我的假设：无" → "无"），空标题自然落进下面的空值判断
    inline = _LABEL_RE.sub("", inline).strip()
    if inline and inline not in _EMPTY_ASSUMPTIONS:
        out.append(_clean_assumption(inline))
    for line in lines[start + 1:]:
        s = line.strip().lstrip("#>*").strip()
        if re.match(r"^6\s*[.、:：)]", s):      # 下一段开始
            break
        s = re.sub(r"^[-*•·]\s*", "", s).strip()
        if not s:
            continue
        if s in _EMPTY_ASSUMPTIONS:
            continue
        out.append(_clean_assumption(s))
        if len(out) >= 10:                      # 结构字段要有上界（防噪声爆炸）
            break
    return out


_EMPTY_ASSUMPTIONS = {"无", "（无）", "(无)", "无。", "None", "none", "-", "——"}

# 行内小标题：「5. 我的假设：…」「5. 假设 - …」里的"假设"两字不是假设内容
_LABEL_RE = re.compile(r"^\*{0,2}(?:我的)?假设\*{0,2}\s*[：:：]?\s*")


def _clean_assumption(text: str) -> str:
    one = " ".join(str(text or "").split())
    return one[:200]


# ---------- ⑤-b 项目硬约定（§1.6：CONSTRAINTS 的落点与加载） ----------

CONSTRAINTS_RELPATH = Path(".agent_godot") / "constraints.md"
MAX_CONSTRAINT_RULES = 20           # 硬上限（§1.6 ⑤-3：约定膨胀会淹没重点）
MAX_CONSTRAINT_CHARS = 800          # 注入文本的字数上限（N 个子代理 = N 倍放大）

# 显式禁令词：只有命中这些词才自动派生可机检规则（不猜，§1.6 ⑤-4 的反面）
_BAN_MARKERS = ("禁止", "禁用", "不许", "不允许", "不得使用", "不要使用",
                "不要用", "弃用")


@dataclass(frozen=True)
class Rule:
    """一条**可机检**的约定（§1.6 ③(d)）：命中假设里的禁止项即违反。

    只查**假设**（任务书没说、子代理自己定的），不查任务书明确要求的东西——
    后者本来就该由 verifier 按验收标准核。
    """

    text: str = ""
    forbid: tuple[str, ...] = ()

    def forbids(self, assumption: str) -> bool:
        low = str(assumption or "").lower()
        return any(k and k.lower() in low for k in self.forbid)


@dataclass
class Constraints:
    """项目硬约定：注入文本（人读）+ 规则表（机检）。

    text ：注入每个子代理的那一段（**无条件**注入，空也要给空标记）
    rules：聚合侧与自报假设求交用；为空 = 冷启动，只自报假设不做比对
           （§1.6 ⑥-6）
    """

    text: str = ""
    rules: list[Rule] = field(default_factory=list)
    updated_at: str = ""
    truncated: bool = False
    source: str = ""

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    @classmethod
    def from_markdown(cls, raw: str, source: str = "") -> "Constraints":
        """解析 constraints.md：body = 注入文本，frontmatter.rules = 机检规则。

        frontmatter 规则语法（一行一条，竖线分隔约定文本与禁止关键词）：
            rules:
              - 存档用 ConfigFile | JSON, json
        省略规则表时，从正文里带**显式禁令词**（禁止/不许/弃用…）的条目派生
        ——只认显式措辞，不做语义猜测：猜错的规则 = 系统性误杀。
        """
        from agent_godot.skills.loader import parse_frontmatter

        data, body = parse_frontmatter(str(raw or ""))
        bullets = [ln.strip()[2:].strip() for ln in body.splitlines()
                   if ln.strip().startswith("- ")]
        truncated = False
        if len(bullets) > MAX_CONSTRAINT_RULES:
            bullets = bullets[:MAX_CONSTRAINT_RULES]
            truncated = True
        text = "\n".join(f"- {b}" for b in bullets)
        if len(text) > MAX_CONSTRAINT_CHARS:
            text = text[:MAX_CONSTRAINT_CHARS].rstrip() + "…"
            truncated = True
        if truncated:
            text += "\n- （约定过多已截断，请人工整理 .agent_godot/constraints.md）"
        rules = _parse_rules(data.get("rules")) or [Rule(b, _derived_bans(b))
                                                    for b in bullets]
        return cls(text=text, rules=[r for r in rules if r.forbid],
                   updated_at=str(data.get("updated_at") or "").strip(),
                   truncated=truncated, source=source)


def load_constraints(root=None) -> Constraints:
    """从项目根读 `.agent_godot/constraints.md`（Binding：项目内、受沙箱约束）。

    读不到 / 解析失败 → 空 Constraints（**约定缺失不是错误**，只是退化为
    "只自报假设不做比对"，并在聚合报告里显式提示——§1.6 ⑥-6 冷启动）。
    """
    try:
        base = Path(root) if root is not None else Path.cwd()
        path = base / CONSTRAINTS_RELPATH
        if not path.exists():
            return Constraints()
        return Constraints.from_markdown(
            path.read_text(encoding="utf-8"), source=str(path))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        logger.warning("读取项目约定失败（按空约定处理）: %s", e)
        return Constraints()


def _parse_rules(value) -> list[Rule]:
    """frontmatter 的 rules 列表 → Rule（`文本 | 禁词1, 禁词2`）。"""
    out: list[Rule] = []
    for item in value or []:
        text, _, bans = str(item).partition("|")
        forbid = tuple(x.strip() for x in bans.split(",") if x.strip())
        if text.strip():
            out.append(Rule(text.strip(), forbid))
    return out


def _derived_bans(text: str) -> tuple[str, ...]:
    """从带显式禁令词的条目里派生禁止关键词（例：…禁止 JSON → ("JSON",)）。"""
    bans: list[str] = []
    for marker in _BAN_MARKERS:
        for chunk in str(text or "").split(marker)[1:]:
            tok = re.split(r"[，,。；;（(：:\s]", chunk.strip(), maxsplit=1)[0].strip()
            if tok and len(tok) <= 20:
                bans.append(tok)
    return tuple(dict.fromkeys(bans))


def _extract_artifacts(session: Session, registry: ToolRegistry) -> list[str]:
    """从子代理的写类工具调用里回收产出物清单（聚合层查文件冲突用）。

    只认写类工具（readonly=False）的路径参数——读过的文件不算产出。
    """
    out: list[str] = []
    for msg in session.messages:
        for call in (msg.tool_calls or []):
            try:
                if registry.get(call.name).meta.readonly:
                    continue
            except KeyError:
                continue
            path = _first_path(call.arguments)
            if path and path not in out:
                out.append(path)
    return out


def _first_path(arguments: str | None) -> str | None:
    try:
        args = json.loads(arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(args, dict):
        return None
    for key in _PATH_KEYS:
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _resolve_llm(spec: SubagentSpec, ctx: dict):
    """角色配模型的三种来源（优先级从高到低）：显式注入 → 工厂 → models.yaml。

    生产路径走 models.yaml（M02 路由表）：spec.model 是 ref（如
    "deepseek/deepseek-reasoner"），与 CLI 的 registry.llm(ref) 同一条路。
    ref 不合法（比如用户手写了裸模型名）→ 回落 ask 主模型，不炸。
    """
    if ctx.get("llm") is not None:
        return ctx["llm"], str(ctx.get("model") or spec.model)
    factory = ctx.get("llm_factory")
    if factory is not None:
        return factory(spec.model), spec.model

    from agent_godot.core import load_registry
    registry = load_registry()
    ref = spec.model
    try:
        registry.get(ref)
    except KeyError:
        ref = registry.route("ask").ref
        logger.warning("子代理 %s 的模型 %r 不是合法 ref，回落 %s",
                       spec.name, spec.model, ref)
    return registry.llm(ref), registry.get(ref).model


# ---------- ⑥ frontmatter → 工具视图 ----------

def tools_view(registry: ToolRegistry, wanted: list[str],
               readonly: bool | None) -> ToolRegistry:
    """工具名与 tag 混写的白名单 → 视图（registry.filter 的语义延伸）。

    白名单项：命中已注册工具名 → 直接收；否则当 tag 处理（"fs"/"godot"）。
    """
    if not wanted:
        return registry.filter(readonly=readonly) if readonly is not None \
            else registry
    view = ToolRegistry()
    names = [w for w in wanted if registry.has(w)]
    tags = {w for w in wanted if w not in names}
    for n in names:
        view.register(registry.get(n))
    if tags:
        tagged = registry.filter(tags=tags)
        for n in tagged.names():
            view.register(tagged.get(n))
    if readonly is not None:
        view = view.filter(readonly=readonly)
    return view


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _as_bool(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "yes", "1", "readonly"):
        return True
    if s in ("false", "no", "0"):
        return False
    return None


def _as_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


__all__ = ["Budget", "Constraints", "SubagentSpec", "SubtaskResult",
           "WhitelistStrategy", "spawn", "tools_view", "DEFAULT_MODEL",
           "DELIVERY_SPEC", "Rule", "load_constraints", "CONSTRAINTS_RELPATH"]
