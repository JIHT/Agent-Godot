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
    """

    spec_name: str
    ok: bool
    report: str                                 # 交付报告（唯一回传物）
    artifacts: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=lambda: Usage(0, 0))
    stop_reason: str = "natural"
    title: str = ""
    attempts: int = 1

    @property
    def tokens(self) -> int:
        return self.usage.input_tokens + self.usage.output_tokens

    def summary(self, limit: int = 200) -> str:
        """聚合视图里的一行摘要（报告截断，防止聚合报告本身爆炸）。"""
        text = " ".join((self.report or "").split())
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
    out = SubtaskResult(
        spec_name=spec.name,
        ok=result.stop_reason == "natural",
        report=result.final_text or "",
        artifacts=_extract_artifacts(session, spec.tools),
        usage=result.usage_total or Usage(0, 0),
        stop_reason=result.stop_reason)
    if bus is not None:
        await bus.emit("subagent_done", spec=spec.name, ok=out.ok,
                       stop_reason=out.stop_reason, tokens=out.tokens)
    # ★ session 在此随栈帧销毁——过程上下文不回传（隔离的命门）
    return out


def _task_prompt(spec: SubagentSpec, task: str, ctx: dict) -> str:
    """任务书 = 角色 + 任务 + （项目现状摘要）+ 交付格式要求。

    §3 难点：任务书必须**自包含**——主控"当然知道"的背景（项目约定/之前的
    对话）子代理一无所知（隔离是双向的）。digest 是补漏通道，交付要求段
    强制报告结构化（否则聚合层拿到一堆自由文本没法做一致性检查）。
    """
    parts = [f"# 角色\n{spec.role_prompt}", f"# 任务书\n{task}"]
    if digest := str(ctx.get("digest") or "").strip():
        parts.append("# 项目现状摘要（主控提供；任务书里没写的通用约定在此）\n"
                     + digest)
    parts.append(
        "# 交付要求\n"
        "完成后直接输出交付报告，结构：\n"
        "1. 结论（完成 / 部分完成 / 无法完成 + 一句话理由）\n"
        "2. 产出清单（新增/修改的文件路径，没有就写「无」）\n"
        "3. 关键决策（技术选型与理由，1~3 条）\n"
        "4. 遗留问题与风险\n"
        "报告控制在 800 字内——只回传结论，过程留在你自己这里。")
    return "\n\n".join(parts)


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


__all__ = ["Budget", "SubagentSpec", "SubtaskResult", "WhitelistStrategy",
           "spawn", "tools_view", "DEFAULT_MODEL"]
