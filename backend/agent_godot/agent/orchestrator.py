"""agent/orchestrator.py —— 包工头：拆解 / 冲突分组 / 并发 / 聚合（M15 §1.3）

四步循环（§1.3 ① 严格定义）：
  ① 拆解 decompose：一次 LLM 调用把任务 → 2~4 个自包含任务书（含验收标准+边界）
  ② 静态冲突检查 resolve_groups：写目标相交的子任务**同组串行**——在派发时
     消灭竞态（编译期检查优于运行时崩溃的同一哲学，§2 问答 3）
  ③ 并发执行 _run_groups：组间并行（asyncio.gather）、组内串行；信号量限并发度
     （每个子代理都在打 LLM API，并发爆表触发限流——M02 令牌桶是全局共享的）
  ④ 聚合 aggregate：报告合并 + 跨任务一致性检查（产出物重叠/空报告/未完成），
     冲突时可选派 verifier 仲裁（§1.3 ③）

失败处置三板斧（§2 问答 5，按失败原因分类路由）：
  - 预算类（max_steps/token/timeout）→ 重派一次（改任务书，加"上一轮中断"说明）
  - 执行性小失败 / 部分完成        → 吞并（报告进聚合，主控自己判断要不要补）
  - 方向性失败（error / 空报告）    → 上抛（落进 conflicts，交回主控/用户决策）

★ multi 模式降级契约（M13 §7.9）：拆解失败（模型没吐出 JSON / 网络挂了）时
自动退化为"单个 coder 子任务"——multi 永远可用，只是并行度降为 1。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from agent_godot.core import LLMRequest, Message, Usage
from agent_godot.tools import ToolRegistry

from .subagents import SubagentSpec, SubtaskResult, spawn

logger = logging.getLogger(__name__)

# 可重派的停止原因（预算类：任务书没毛病，只是工人体力不够）
RETRYABLE = ("max_steps", "token_budget", "usd_budget", "timeout",
             "loop_detected")

RETRY_HINT = ("\n\n【重派说明】上一轮执行在预算内未跑完就被中断（不是你的错）。"
              "请直接给出：1) 已完成的部分 2) 尚未完成的部分 3) 继续做的最小步骤。"
              "不要重复已完成的工作。")


# ---------- ① 拆解提示（§3：决定 multi 上限的一段 prompt） ----------

DECOMPOSE_PROMPT = """你是任务编排者。把用户任务拆解为 2~4 个子任务，输出 JSON：
[{{"title": "...", "brief": "给子代理的完整任务书：目标+边界(只许动哪些文件/目录)+
   验收标准(可检查的完成判据)", "spec": "explorer|coder|verifier",
   "write_targets": ["..."], "depends": ["其他子任务title或空"]}}]
拆解原则：
- 探查先行：不熟悉项目时第一个子任务必须是 explorer 勘察
- 写目标不相交：两个子任务不许写同一个文件（做不到就串行 depends）
- 验收独立：最后一个子任务建议是 verifier 全局验收
- 每份任务书自包含：子代理看不到全局对话，缺的信息写进 brief

只输出 JSON（不要多余文字，可用 ```json 围栏）。

用户任务：{task}
项目现状摘要：{digest}"""


# ---------- ② 数据结构 ----------

@dataclass
class Subtask:
    """一个子任务：任务书 + 角色 + 写目标 + 依赖（编排层的工单）。"""

    title: str = ""
    task_brief: str = ""
    spec: SubagentSpec | None = None       # 显式指定时优先（否则按 spec_name 查表）
    spec_name: str = "coder"
    write_targets: set[str] = field(default_factory=set)
    depends: list[str] = field(default_factory=list)
    retries: int = 0


@dataclass
class OrchestrResult:
    """编排结果：聚合报告 + 每子任务 usage 汇总 + 冲突清单。

    stop_reason / steps 与 M03 LoopResult 同名字段——cli 打印与前端渲染
    不必区分"单代理跑完"和"编排跑完"两种结果类型（鸭子类型兼容）。
    """

    task: str = ""
    report: str = ""
    results: list[SubtaskResult] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=lambda: Usage(0, 0))
    groups: list[list[str]] = field(default_factory=list)
    arbitration: str = ""
    stop_reason: str = "natural"

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results) and not self.conflicts

    @property
    def steps(self) -> int:
        return len(self.results)


# ---------- ③ 编排器 ----------

class Orchestrator:
    """包工头：拆解 → 冲突分组 → 并发派发 → 聚合交付。"""

    def __init__(self, llm, specs: dict[str, SubagentSpec],
                 registry: ToolRegistry | None = None, bus=None, *,
                 max_parallel: int = 3, max_retries: int = 1,
                 auto_arbitrate: bool = True, fallback_spec: str = "coder",
                 model: str = "orchestrator", session_ctx: dict | None = None):
        self.llm = llm
        self.specs = dict(specs)
        self.registry = registry
        self.bus = bus
        self.max_parallel = max(1, max_parallel)
        self.max_retries = max(0, max_retries)
        self.auto_arbitrate = auto_arbitrate
        self.fallback_spec = fallback_spec
        self.model = model                      # 记账用标记（非真实模型名）
        self.session_ctx = dict(session_ctx or {})

    # ---------- 主入口 ----------

    async def run(self, session, task: str,
                  subtasks: list[Subtask] | None = None) -> OrchestrResult:
        """任务 → 编排结果（multi 模式的外循环，类比 plan 的 run_plan_mode）。

        进主控会话的只有两样：**用户任务**与**聚合报告**——子代理的过程
        消息一条都不进（loop.run 的记账习惯在此保持一致，否则 /rewind
        回放时会缺"用户到底要了什么"这一环）。

        subtasks：跳过拆解直接派发（测试 / 外部已有工单时的注入口）。
        """
        append = getattr(session, "append", None)
        if callable(append):
            append(Message(role="user", content=task))
        digest = self._digest(session)
        if subtasks is None:
            subtasks = await self.decompose(task, digest=digest)
        groups = self.resolve_groups(subtasks)
        await self._emit("orchestrator_plan", task=task,
                         subtasks=[s.title for s in subtasks],
                         groups=[[s.title for s in g] for g in groups])

        ctx = self._spawn_ctx(digest)
        results = await self._run_groups(groups, ctx)

        order = {s.title: i for i, s in enumerate(subtasks)}
        results.sort(key=lambda r: order.get(r.title, len(order)))

        out = await self.aggregate(results, task=task)
        out.groups = [[s.title for s in g] for g in groups]
        # 主控上下文只收**聚合报告**（不是各子代理的过程数据——隔离的命门）
        if callable(append) and out.report:
            append(Message(role="assistant", content=out.report))
        await self._emit("orchestrator_done", ok=out.ok,
                         subtasks=len(out.results),
                         conflicts=len(out.conflicts),
                         tokens=out.usage.input_tokens + out.usage.output_tokens)
        return out

    # ---------- ① 拆解 ----------

    async def decompose(self, task: str, digest: str = "") -> list[Subtask]:
        """任务 → 子任务清单（一次 LLM 调用，输出 JSON）。

        拆解失败（模型吐不出 JSON / 调用异常）→ 降级为单 coder 子任务：
        multi 模式永远可用，最坏情况退化为"单代理跑一次"（M13 降级契约）。
        """
        prompt = DECOMPOSE_PROMPT.format(task=task, digest=digest or "（无）")
        try:
            resp = await self.llm.complete(LLMRequest(
                model=self.model,
                messages=[Message(role="user", content=prompt)],
                temperature=0.2, stream=False))
            parsed = self._parse_subtasks(resp.content if resp else "")
        except Exception as e:                  # noqa: BLE001 —— 拆解失败不许炸
            logger.warning("任务拆解失败，降级为单子任务: %s", e)
            await self._emit("error", error=f"任务拆解失败: {e}")
            parsed = []
        return parsed or [Subtask(title=_short(task),
                                  task_brief=task,
                                  spec_name=self.fallback_spec)]

    def _parse_subtasks(self, content: str) -> list[Subtask]:
        """JSON → Subtask 列表（容忍围栏/前后废话/字段缺失）。"""
        data = _load_json(content)
        if isinstance(data, dict):
            data = data.get("subtasks") or data.get("tasks") or []
        if not isinstance(data, list):
            return []
        out: list[Subtask] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or f"子任务{i + 1}").strip()
            brief = str(item.get("brief") or item.get("task_brief")
                        or item.get("description") or "").strip()
            if not brief:
                continue
            out.append(Subtask(
                title=title, task_brief=brief,
                spec_name=str(item.get("spec") or self.fallback_spec).strip(),
                write_targets={str(t) for t in _as_list(item.get("write_targets"))},
                depends=[str(d) for d in _as_list(item.get("depends"))]))
        return out

    # ---------- ② 静态冲突检查：写目标分组（§1.3 ③） ----------

    def resolve_groups(self, subtasks: list[Subtask]) -> list[list[Subtask]]:
        """写文件集相交的子任务必须同组串行（否则并行写同一文件=竞态）。

        组语义：**组间并行、组内串行**。两条约束都靠"合并进同一组"实现：
          - 写目标相交 → 同组排队（§1.3 ③ 原样）
          - depends     → 与被依赖者同组（且排在其后）——组间是并发的，
            只把依赖者放进"更晚的组"并不能保证先后（两个组同时开跑），
            必须合并成一条串行链（§4 步骤 3：写目标分组合并 depends）

        依赖跨多个组时（c 依赖 a 与 b，a/b 在不同组）→ 把这几个组合并，
        保证 c 在两个前驱都跑完之后才开始。
        """
        groups: list[list[Subtask]] = []
        where: dict[str, int] = {}              # title → 组下标
        for st in self._dependency_order(subtasks):
            must = {where[d] for d in st.depends if d in where}
            must |= {i for i, g in enumerate(groups)
                     if st.write_targets & {t for s in g for t in s.write_targets}}
            if not must:                        # 无冲突无依赖 → 独立成组（并行）
                groups.append([st])
            else:
                target = min(must)
                merged: list[Subtask] = []
                for idx in sorted(must, reverse=True):
                    merged = groups.pop(idx) + merged   # 小下标在前，保序
                merged.append(st)
                groups.insert(target, merged)
            where = {s.title: gi for gi, g in enumerate(groups) for s in g}
        return groups

    @staticmethod
    def _dependency_order(subtasks: list[Subtask]) -> list[Subtask]:
        """依赖拓扑序（Kahn）；未知依赖忽略、有环时剩余按原序补上（不抛）。"""
        titles = [s.title for s in subtasks]
        by_title = {s.title: s for s in subtasks}
        deps = {s.title: [d for d in s.depends if d in by_title]
                for s in subtasks}
        indeg = {t: len(set(deps[t])) for t in titles}
        children: dict[str, list[str]] = {t: [] for t in titles}
        for t, ds in deps.items():
            for d in set(ds):
                children[d].append(t)
        queue = [t for t in titles if indeg[t] == 0]
        order: list[Subtask] = []
        while queue:
            u = queue.pop(0)
            order.append(by_title[u])
            for v in children[u]:
                indeg[v] -= 1
                if indeg[v] == 0:
                    queue.append(v)
        if len(order) < len(titles):            # 有环：剩下的按原序补（尽力而为）
            done = {s.title for s in order}
            order.extend(s for s in subtasks if s.title not in done)
        return order

    # ---------- ③ 并发派发 ----------

    async def _run_groups(self, groups: list[list[Subtask]],
                          ctx: dict) -> list[SubtaskResult]:
        """组间并行、组内串行；信号量把同时在跑的子代理压到 max_parallel。"""
        sem = asyncio.Semaphore(self.max_parallel)

        async def run_group(group: list[Subtask]) -> list[SubtaskResult]:
            out: list[SubtaskResult] = []
            for st in group:                    # 组内串行（写目标相交 / 依赖链）
                async with sem:                 # 并发度上限（防 LLM 限流）
                    out.append(await self._run_subtask(st, ctx))
            return out

        gathered = await asyncio.gather(
            *(run_group(g) for g in groups), return_exceptions=True)
        results: list[SubtaskResult] = []
        for group, item in zip(groups, gathered):
            if isinstance(item, BaseException):
                # 单个子代理炸了不能掀桌子：转成失败报告进聚合（§1.3 失败处理）
                for st in group:
                    results.append(SubtaskResult(
                        spec_name=st.spec_name, ok=False, title=st.title,
                        report=f"子代理执行异常: {type(item).__name__}: {item}",
                        stop_reason="error"))
            else:
                results.extend(item)
        return results

    async def _run_subtask(self, st: Subtask, ctx: dict) -> SubtaskResult:
        """派一个工单（含重派一次的重试策略）。"""
        spec = st.spec or self.specs.get(st.spec_name) or \
            self.specs.get(self.fallback_spec) or _first_spec(self.specs)
        if spec is None:
            raise RuntimeError("没有任何可用子代理角色（specs 为空）")
        await self._emit("subtask_start", title=st.title, spec=spec.name,
                         write_targets=sorted(st.write_targets))
        result = await spawn(spec, st.task_brief, ctx)
        result.title = st.title
        while (not result.ok and result.stop_reason in RETRYABLE
               and st.retries < self.max_retries):
            st.retries += 1
            await self._emit("subtask_retry", title=st.title,
                             attempt=st.retries, reason=result.stop_reason)
            result = await spawn(spec, st.task_brief + RETRY_HINT, ctx)
            result.title = st.title
            result.attempts = st.retries + 1
        return result

    # ---------- ④ 聚合 ----------

    async def aggregate(self, results: list[SubtaskResult],
                        task: str = "") -> OrchestrResult:
        """报告合并 + 一致性检查（冲突可触发 verifier 仲裁）。

        聚合**不是拼接**：报告矛盾（一个说完成一个说部分完成）、产出物撞车
        必须显式列出，静默拼接 = 埋雷（§1.3 易错点③）。
        """
        conflicts = self.find_conflicts(results)
        usage = Usage(
            input_tokens=sum(r.usage.input_tokens for r in results),
            output_tokens=sum(r.usage.output_tokens for r in results),
            cost_usd=round(sum(r.usage.cost_usd for r in results), 6))
        arbitration = ""
        if conflicts and self.auto_arbitrate and self.llm is not None \
                and "verifier" in self.specs:
            try:
                arbitration = await self.arbitrate(results, conflicts)
            except Exception as e:              # noqa: BLE001 —— 仲裁失败不拖垮交付
                arbitration = f"（仲裁子代理执行失败: {e}）"
        report = self._render(task, results, conflicts, arbitration, usage)
        return OrchestrResult(
            task=task, report=report, results=list(results),
            conflicts=conflicts, usage=usage, arbitration=arbitration,
            stop_reason="natural" if not conflicts else "partial")

    def find_conflicts(self, results: list[SubtaskResult]) -> list[str]:
        """跨子任务静态一致性检查（§1.3 ③）：产出物重叠 / 空报告 / 未完成。"""
        conflicts: list[str] = []
        owner: dict[str, list[str]] = {}
        for r in results:
            for artifact in r.artifacts:
                owner.setdefault(artifact, []).append(r.title or r.spec_name)
        for artifact, owners in owner.items():
            if len(owners) > 1:                 # 两个子代理都产出同一文件
                conflicts.append("产出物冲突："
                                 f"{artifact} 被 {' / '.join(owners)} 同时改动")
        for r in results:
            if not r.report.strip():
                conflicts.append(f"子任务「{r.title or r.spec_name}」"
                                 "未产出交付报告（无法验收）")
            elif not r.ok:
                conflicts.append(
                    f"子任务「{r.title or r.spec_name}」未完成"
                    f"（{r.stop_reason}，尝试 {r.attempts} 次）")
        return conflicts

    async def arbitrate(self, results: list[SubtaskResult],
                        conflicts: list[str]) -> str:
        """派 verifier 做一次仲裁（合并还是删一——只给意见，不改文件）。"""
        brief = ("以下是多个子代理的交付报告与发现的冲突，请给出仲裁意见：\n\n"
                 "【冲突清单】\n" + "\n".join(f"- {c}" for c in conflicts)
                 + "\n\n【各子任务报告】\n"
                 + "\n\n".join(f"## {r.title or r.spec_name}（{r.spec_name}）\n"
                               f"{r.report}" for r in results)
                 + "\n\n【要求】输出：1) 冲突定性与优先级 2) 每个冲突的处置建议"
                   "（保留哪个/如何合并/谁返工）3) 最终是否可交付。只给结论，"
                   "不要修改任何文件。")
        result = await spawn(self.specs["verifier"], brief, self._spawn_ctx(""))
        return result.report

    # ---------- 视图与辅助 ----------

    @staticmethod
    def _render(task: str, results: list[SubtaskResult], conflicts: list[str],
                arbitration: str, usage: Usage) -> str:
        lines: list[str] = []
        if task:
            lines.append(f"# 任务\n{task}\n")
        lines.append(f"# 子任务交付（{len(results)} 个）")
        for r in results:
            lines.append(f"- {r.summary()}")
            if r.artifacts:
                lines.append(f"    产出: {', '.join(r.artifacts[:10])}")
        lines.append("")
        if conflicts:
            lines.append("# 冲突（需人工确认或返工）")
            lines.extend(f"- {c}" for c in conflicts)
        else:
            lines.append("# 冲突\n- 无（跨子任务一致性检查通过）")
        if arbitration:
            lines.append(f"\n# 仲裁意见（verifier）\n{arbitration}")
        total = usage.input_tokens + usage.output_tokens
        lines.append(f"\n# 用量\n- token: {usage.input_tokens} 入 + "
                     f"{usage.output_tokens} 出 = {total}"
                     f"（成本约 ${usage.cost_usd:.4f}）")
        return "\n".join(lines)

    def _digest(self, session) -> str:
        """项目现状摘要（§3 的 digest 参数）：主控对话尾 + 可用工具清单。

        这是"任务书自包含"的补漏通道（§2 问答 8 防漏三招之一）——子代理
        看不到主控对话，约定类信息只能靠这里显式传递。
        """
        parts: list[str] = []
        msgs = list(getattr(session, "messages", None) or [])
        tail = [m for m in msgs if m.role in ("user", "assistant")][-6:]
        if tail:
            parts.append("最近对话要点:\n" + "\n".join(
                f"- {m.role}: {' '.join((m.content or '').split())[:200]}"
                for m in tail))
        if self.registry is not None:
            names = self.registry.names()
            parts.append("可用工具: " + ", ".join(names[:40])
                         + ("…" if len(names) > 40 else ""))
        return "\n".join(parts)

    def _spawn_ctx(self, digest: str) -> dict:
        ctx = dict(self.session_ctx)
        ctx.setdefault("bus", self.bus)
        if digest:
            ctx["digest"] = digest
        return ctx

    async def _emit(self, type_: str, **payload) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.emit(type_, **payload)
        except Exception:                       # noqa: BLE001 —— 事件不许拖垮执行
            pass


# ---------- 小工具 ----------

def _load_json(text: str):
    """从模型输出里抠出 JSON（容忍 ```json 围栏 / 前后废话）。"""
    raw = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        start, end = raw.find("["), raw.rfind("]")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
        else:
            start, end = raw.find("{"), raw.rfind("}")
            if start < 0 or end <= start:
                raise ValueError(f"模型输出不是合法 JSON: {raw[:120]!r}")
            raw = raw[start:end + 1]
    return json.loads(raw)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value] if str(value).strip() else []


def _short(text: str, limit: int = 30) -> str:
    one_line = " ".join((text or "").split())
    return one_line[:limit] or "主任务"


def _first_spec(specs: dict[str, SubagentSpec]) -> SubagentSpec | None:
    return next(iter(specs.values()), None)


__all__ = ["DECOMPOSE_PROMPT", "OrchestrResult", "Orchestrator", "Subtask",
           "RETRYABLE", "RETRY_HINT"]
