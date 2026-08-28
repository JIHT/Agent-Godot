"""agent/paradigms/multi.py —— multi 模式骨架（M13 §4 步骤 5 / 面试 §7.9）

契约档位：per_worker 工具视图 + 任务级审批 + **并行编排形态**。

★ 与 Cursor 的差异（面试 §7.10）：Cursor **没有** Multi 模式——它的多 agent
并行能力藏在 Agents Window 里（3.0+：每个 agent tab 持有自己的
mode/model/worktree；3.2 的 /multitask 让异步子代理并行而非排队）。
本项目把它**产品化提到模式切换器上**做成显式模式：可发现性 > 概念纯度。

注意 multi 不是"能力档位"维度，而是"编排形态"维度——它和 craft 不是并列
的能力等级，而是"同样的能力，换并行执行"。

★ 范式说明（M13 §1.3）：multi 内部按需组合全部四个范式——ReAct（底座）、
Reflection（verify="per_task"）、Plan-and-Solve（plan_first=True）、
Multi-Agent（M15 Orchestrator）。

车队模式占位：M13 只交付"模式注册 + 单代理降级路径"，真正的 Orchestrator-
Worker 分发器、子代理上下文隔离、结果聚合、A2A 协议留给 M15。

分界理由（§7.9）：模式引擎（配置/注册/钩子）与并行执行器（进程/任务管理）
是正交关注点——M13 完成时 multi 可用（串行版：降级为 plan 串行），
M15 完成时变并行（接口不变）。config.tools="per_worker" 由 tools_view
原样放行，子代理级白名单的分发逻辑在 M15 的 Orchestrator 里做。
"""
from __future__ import annotations

from .base import ModeConfig, ModeStrategy, register


@register
class MultiStrategy(ModeStrategy):
    mode = "multi"
    config = ModeConfig(tools="per_worker", temperature=0.3,
                        verify="per_task", plan_first=True)

    def __init__(self, llm=None, loop=None,
                 config: ModeConfig | None = None, **kwargs):
        super().__init__(config, **kwargs)
        self.llm = llm
        self.loop = loop


__all__ = ["MultiStrategy"]
