"""agent/paradigms/multi.py —— multi 模式：车队契约 + Orchestrator 接线
（M13 §4 步骤 5 / M15 §1.3，面试 §7.9 / §7.10）

契约档位：per_worker 工具视图 + 任务级审批 + **并行编排形态**。

★ 与 Cursor 的差异（面试 §7.10）：Cursor **没有** Multi 模式——它的多 agent
并行能力藏在 Agents Window 里（3.0+：每个 agent tab 持有自己的
mode/model/worktree；3.2 的 /multitask 让异步子代理并行而非排队）。
本项目把它**产品化提到模式切换器上**做成显式模式：可发现性 > 概念纯度。

注意 multi 不是"能力档位"维度，而是"编排形态"维度——它和 craft 不是并列
的能力等级，而是"同样的能力，换并行执行"。

★ 范式说明（M13 §1.3）：multi 内部按需组合全部四个范式——ReAct（底座）、
Reflection（verify="per_task"，节点边界由 Orchestrator 触发）、
Plan-and-Solve（plan_first=True，Orchestrator.decompose 就是"隐式 DAG"）、
Multi-Agent（本文件接线的 Orchestrator）。

M13 → M15 的交接（§7.9）：M13 交付"模式注册 + 单代理降级路径"，M15 交付
Orchestrator-Worker 分发器。接口不变——`config.tools="per_worker"` 仍由
tools_view 原样放行，子代理级白名单的分发在 Orchestrator 里做：
    loop.run(mode="multi")              → 单代理降级（M13 行为，不派发）
    MultiStrategy.run_multi_mode(...)   → 真并行编排（M15 行为，CLI 走这条）
两条路径并存是刻意的：编排器需要 llm/registry/bus 三件套，缺任一都能降级，
multi 模式不会出现"选了却跑不起来"。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import ModeConfig, ModeStrategy, register

if TYPE_CHECKING:                     # 只做类型提示（避免 import 环）
    from ..orchestrator import OrchestrResult


@register
class MultiStrategy(ModeStrategy):
    mode = "multi"
    config = ModeConfig(tools="per_worker", temperature=0.3,
                        verify="per_task", plan_first=True)

    def __init__(self, llm=None, loop=None, specs: dict | None = None,
                 registry=None, bus=None, max_parallel: int = 3,
                 config: ModeConfig | None = None, project_root=None,
                 checkpoints=None, approver=None, **kwargs):
        super().__init__(config, **kwargs)
        self.llm = llm
        self.loop = loop
        self.specs = specs
        self.registry = registry
        self.bus = bus
        self.max_parallel = max_parallel
        self.orchestrator = None
        # M15 第二轮加固（§1.4/§1.6）：编排器判定冲突要用项目根（受保护名单、
        # 大小写折叠、CONSTRAINTS 加载、检查点快照都挂在它上面）
        self.project_root = project_root
        self.checkpoints = checkpoints
        self.approver = approver

    # ---------- 装配（惰性：缺资源也能降级跑单代理） ----------

    def build_orchestrator(self):
        """按需组装 Orchestrator（specs/registry 缺失时从 loop 上取）。"""
        if self.orchestrator is not None:
            return self.orchestrator
        # 惰性 import：paradigms 包在 __init__ 里就导入本模块，若此处顶层
        # import orchestrator 会与 subagents → paradigms.base 形成环。
        from ..orchestrator import Orchestrator
        from ..subagents.builtin import build_default_specs

        registry = self.registry or getattr(
            getattr(self.loop, "dispatcher", None), "registry", None)
        if registry is None:
            raise RuntimeError(
                "multi 模式需要工具注册表（注入 registry 或传入已装配的 loop）")
        llm = self.llm or getattr(self.loop, "llm", None)
        if llm is None:
            raise RuntimeError(
                "multi 模式需要 LLM（任务拆解依赖它；注入 llm 或传入 loop）")
        bus = self.bus or getattr(self.loop, "bus", None)
        specs = self.specs or build_default_specs(registry)
        self.orchestrator = Orchestrator(
            llm, specs, registry, bus, max_parallel=self.max_parallel,
            project_root=self.project_root, checkpoints=self.checkpoints,
            approver=self.approver)
        return self.orchestrator

    # ---------- 编排入口（CLI / 应用端走这条） ----------

    async def run_multi_mode(self, session, task: str) -> "OrchestrResult":
        """派活给子代理车队：拆解 → 冲突分组 → 并发 → 聚合。

        与 plan 的 run_plan_mode 对称：两者都是"挂在 Loop 之外的外循环"，
        Loop 本身仍是那个 ReAct 底座（只是这里被 spawn 了 N 份）。
        """
        return await self.build_orchestrator().run(session, task)


__all__ = ["MultiStrategy"]
