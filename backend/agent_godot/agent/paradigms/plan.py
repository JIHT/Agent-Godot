"""agent/paradigms/plan.py —— plan 模式：架构师契约 + Plan-and-Solve 外循环
（M13 §1.5 / §3 / §4 步骤 4）

契约档位：全工具（规划期只研究不写）+ 任务级审批门（DAG 审批）。

★ 范式说明（M13 §1.3）：plan ≠ Plan-and-Solve。plan 是模式（契约），
本文件的 DAG 外循环才是 Plan-and-Solve 范式的一次**显式**启用。

一次 plan 模式执行同时用到三个范式——这是"模式 ⊥ 范式"的铁证：
- Plan-and-Solve：本文件的外层 DAG 生成 + 人审批 + 拓扑推进
- ReAct：每个 DAG 节点通过 loop.run(mode="craft") 开子循环（见 run_plan_mode）
- Reflection：节点内每次写操作触发 craft 的 VerifyLoop 校验

先画施工图再盖楼：模型先产出任务 DAG（节点=子任务，边=依赖），经人审批后
按拓扑序执行；节点失败触发 re-plan（带"已完成产出摘要 + 失败上下文"重排
剩余子图，不推倒已完成）。三条状态流拧在一起（§3 为什么难）：
  ① 推进条件：ready 空 ≠ 结束（可能是失败节点死锁阻塞）
  ② re-plan 上下文：已完成节点产出摘要 + 失败原因 + 原始目标
  ③ 审批拒绝：干净退出（不残留半执行状态）

人审批是任务级 HITL（对比 M09 的操作级确认门）：节点粒度 / 完成判据 / 依赖结构。
"""
from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

from agent_godot.core import LLMRequest, Message

from .base import ModeConfig, ModeStrategy, register


class PlanCycleError(Exception):
    """DAG 带环（A 依赖 B 依赖 A）——解析时拓扑检测，带环直接打回重规划。"""


@dataclass
class PlanNode:
    """DAG 节点：一个子任务。criterion 是"完成判据"（给 re-plan / 审批判断用）。"""

    id: str
    title: str
    description: str = ""
    depends: list[str] = field(default_factory=list)
    status: Literal["pending", "running", "done", "failed", "skipped"] = "pending"
    max_retries: int = 1
    criterion: str = ""

    def prompt(self, done_summary: str = "") -> str:
        """把节点渲染成喂给子循环（craft）的任务描述。"""
        lines = [f"任务「{self.title}」", self.description or self.title]
        if self.criterion:
            lines.append(f"完成判据：{self.criterion}")
        if done_summary:
            lines.append(f"已完成节点的产出摘要（不要重复做）：\n{done_summary}")
        return "\n".join(lines)


# 计划审批回调：async (审批文本) -> 是否批准（CLI 交互 / 测试注入）
PlanApprover = Callable[[str], Awaitable[bool]]


class PlanGraph:
    """任务 DAG：校验无环 + 拓扑推进 + re-plan 子图重排。"""

    def __init__(self, nodes: list[PlanNode]):
        self.nodes: dict[str, PlanNode] = {n.id: n for n in nodes}
        self.validate_acyclic()

    # ---------- 生成 ----------

    @classmethod
    async def from_task(cls, llm, task: str) -> "PlanGraph":
        """任务 → JSON DAG 提示 → 解析 + 校验无环（§1.1 ③ / §4 步骤 4）。"""
        req = LLMRequest(
            model="plan-mode",
            messages=[Message(role="user", content=_PLAN_PROMPT.format(task=task))],
            temperature=0.2, stream=False)
        resp = await llm.complete(req)
        data = _parse_dag(resp.content)
        nodes = [_node_from_dict(d) for d in data.get("nodes", [])]
        if not nodes:
            raise ValueError("模型未产出任何计划节点（JSON 里 nodes 为空）")
        return cls(nodes)

    # ---------- 校验 ----------

    def validate_acyclic(self) -> None:
        """三色 DFS 检环 + 依赖引用存在性校验。带环抛 PlanCycleError。"""
        for n in self.nodes.values():
            for d in n.depends:
                if d not in self.nodes:
                    raise PlanCycleError(f"节点 {n.id} 依赖不存在的节点 {d!r}")

        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {nid: WHITE for nid in self.nodes}

        def dfs(u: str, stack: list[str]) -> None:
            color[u] = GRAY
            stack.append(u)
            for d in self.nodes[u].depends:
                if color[d] == GRAY:
                    cycle = stack[stack.index(d):] + [d]
                    raise PlanCycleError("计划存在循环依赖: " + " → ".join(cycle))
                if color[d] == WHITE:
                    dfs(d, stack)
            stack.pop()
            color[u] = BLACK

        for nid in self.nodes:
            if color[nid] == WHITE:
                dfs(nid, [])

    # ---------- 拓扑推进 ----------

    def ready_nodes(self) -> list[PlanNode]:
        """依赖全部 done 的 pending 节点（拓扑推进的核心，§1.1 ③）。"""
        return [n for n in self.nodes.values()
                if n.status == "pending"
                and all(self.nodes[d].status == "done" for d in n.depends)]

    def mark(self, node_id: str, status: str) -> None:
        self.nodes[node_id].status = status  # type: ignore[assignment]

    def all_done(self) -> bool:
        return all(n.status in ("done", "skipped") for n in self.nodes.values())

    def failed_nodes(self) -> list[PlanNode]:
        return [n for n in self.nodes.values() if n.status == "failed"]

    def done_summary(self) -> str:
        """已完成节点的产出摘要（re-plan 上下文三要素之一）。"""
        lines = [f"- [{n.id}] {n.title}：{n.criterion or n.description}"
                 for n in self.nodes.values() if n.status == "done"]
        return "\n".join(lines) if lines else "（无已完成节点）"

    # ---------- re-plan ----------

    async def replan_from(self, llm, failed: PlanNode) -> None:
        """带上下文重排"失败节点及其后代"子图；前驱（done）保持不动。

        上下文三样（面试 §7.4）：① 已完成节点清单+产出摘要 ② 失败节点完整
        错误上下文 ③ 原始任务目标（由调用方在 from_task 时已确立，此处只
        补前两样）。前驱保持 done，不会被重复执行。
        """
        context = (f"已完成节点及产出摘要：\n{self.done_summary()}\n\n"
                   f"失败节点：{failed.id}「{failed.title}」\n"
                   f"失败原因/上下文：\n{failed.description or failed.criterion}\n")
        req = LLMRequest(
            model="plan-mode",
            messages=[Message(role="user",
                              content=_REPLAN_PROMPT.format(context=context))],
            temperature=0.2, stream=False)
        resp = await llm.complete(req)
        data = _parse_dag(resp.content)

        new_nodes = {d.get("id"): d for d in data.get("nodes", [])}
        affected = self._descendants(failed.id)
        for nid in affected:
            if nid not in new_nodes:
                continue
            d = new_nodes[nid]
            node = self.nodes[nid]
            node.title = str(d.get("title", node.title))
            node.description = str(d.get("description", node.description))
            node.criterion = str(d.get("criterion",
                                      d.get("done_when", node.criterion)))
            node.depends = [x for x in d.get("depends", []) if x in self.nodes]
            node.status = "pending"
        self.validate_acyclic()

    def _descendants(self, node_id: str) -> set[str]:
        """失败节点 + 依赖它的所有后代（BFS，闭包）。"""
        out = {node_id}
        changed = True
        while changed:
            changed = False
            for n in self.nodes.values():
                if n.id not in out and any(d in out for d in n.depends):
                    out.add(n.id)
                    changed = True
        return out

    # ---------- 审批视图 ----------

    def render_for_approval(self) -> str:
        """人看的审批视图：拓扑序 + 每节点依赖/完成判据（§4 步骤 4）。"""
        lines = ["任务计划 DAG（批准前请核对节点粒度与完成判据）："]
        for n in self._topo_order():
            deps = f"（依赖: {', '.join(n.depends)}）" if n.depends else ""
            lines.append(f"  - [{n.id}] {n.title}{deps}")
            if n.criterion:
                lines.append(f"        完成判据: {n.criterion}")
        return "\n".join(lines)

    def _topo_order(self) -> list[PlanNode]:
        """Kahn 拓扑序（validate_acyclic 已保证无环）。"""
        indeg = {nid: len(n.depends) for nid, n in self.nodes.items()}
        children: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for n in self.nodes.values():
            for d in n.depends:
                children[d].append(n.id)
        q = deque(nid for nid, d in indeg.items() if d == 0)
        order: list[PlanNode] = []
        while q:
            u = q.popleft()
            order.append(self.nodes[u])
            for v in children[u]:
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)
        return order


# ---------- 计划提示模板 ----------

_PLAN_PROMPT = """你是任务规划器。把用户任务分解为 3~7 个子任务，输出 JSON DAG。

要求：
- 粒度 3~7 个节点（太细 = DAG 里的 for 循环，浪费规划；太粗 = 失去并行机会）
- 每个节点写"完成判据"（criterion，必须是可验证的：文件存在 / 校验通过 / 测试绿）
- depends 引用其他节点的 id（无依赖给空数组）
- 不要形成循环依赖；依赖引用的节点必须真实存在

只输出 JSON（不要多余文字），格式：
{{"nodes": [{{"id": "1", "title": "...", "description": "...", "depends": [], "criterion": "..."}}]}}

任务：{task}
"""

_REPLAN_PROMPT = """你是任务规划器。以下是已完成节点与一个失败节点的上下文，
请只重新规划"失败节点及其后续依赖它的节点"（已完成节点不要动、不要重复执行）。

{context}

只输出 JSON（保持节点 id 与原计划一致，只重排失败节点及其后代），格式：
{{"nodes": [{{"id": "...", "title": "...", "description": "...", "depends": [], "criterion": "..."}}]}}
"""


# ---------- 解析 ----------

def _parse_dag(content: str) -> dict:
    """从模型输出里抠出 JSON 对象（容忍 ```json 围栏 / 前后废话）。"""
    text = (content or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"模型输出不是合法 JSON DAG: {content[:120]!r}")
        text = text[start:end + 1]
    return json.loads(text)


def _node_from_dict(d: dict) -> PlanNode:
    if not isinstance(d, dict):
        raise ValueError(f"计划节点必须是对象，得到 {type(d).__name__}")
    return PlanNode(
        id=str(d.get("id", "")),
        title=str(d.get("title", "")),
        description=str(d.get("description", "")),
        depends=[str(x) for x in (d.get("depends") or [])],
        criterion=str(d.get("criterion", d.get("done_when", ""))),
    )


# ---------- PlanStrategy ----------

@register
class PlanStrategy(ModeStrategy):
    mode = "plan"
    config = ModeConfig(tools="all", temperature=0.2, verify="L1+", plan_first=True)

    def __init__(self, llm=None, loop=None,
                 approver: PlanApprover | None = None,
                 config: ModeConfig | None = None, **kwargs):
        super().__init__(config, **kwargs)
        self.llm = llm
        self.loop = loop
        self.approver = approver

    async def run_plan_mode(self, session, task: str):
        """§3 参考片段：DAG 外循环（挂在 Loop 之外的一层外循环）。

        拓扑推进 + 失败 re-plan + 审批拒绝干净退出三条流交织：
        从任务生成 DAG → 无环校验 → 人审批 → 逐步执行（每个节点用 craft
        子循环，复用验证回路）→ 失败节点 re-plan 重排剩余子图。
        """
        if self.llm is None or self.loop is None:
            raise RuntimeError("PlanStrategy 需要 llm 与 loop（run_plan_mode 依赖）")
        from agent_godot.agent.loop import LoopResult

        graph = await PlanGraph.from_task(self.llm, task)
        graph.validate_acyclic()

        if self.approver is not None:
            approved = await self.approver(graph.render_for_approval())
            if not approved:
                return LoopResult(
                    "计划未获批准，已干净退出（无任何文件改动）",
                    0, None, "error")

        while True:
            ready = graph.ready_nodes()
            if not ready and graph.all_done():
                return LoopResult("全部节点完成", 0, None, "natural")
            if not ready:
                failed = next((n for n in graph.nodes.values()
                               if n.status == "failed"), None)
                if failed is None:
                    return LoopResult(
                        "计划死锁：无就绪节点且无失败节点可 re-plan", 0, None, "error")
                await graph.replan_from(self.llm, failed)
                continue
            for node in ready:                        # 无依赖节点（M15 前先串行）
                graph.mark(node.id, "running")
                result = await self.loop.run(
                    session, node.prompt(graph.done_summary()), mode="craft")
                graph.mark(node.id,
                           "done" if result.stop_reason == "natural" else "failed")


__all__ = ["PlanCycleError", "PlanNode", "PlanGraph", "PlanApprover",
           "PlanStrategy"]
