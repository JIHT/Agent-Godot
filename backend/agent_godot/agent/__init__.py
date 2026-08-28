"""agent：Agent Runtime 执行引擎（M03）—— ReAct 循环、四维预算、事件流、工具调度。

★ 术语分层（M13 §1.3，别写错）：
- 模式（ask/craft/plan/multi）= 产品层的"人机协作契约"，落在
  `agent/paradigms/`（该包名虽叫 paradigms，装的其实是 modes 的策略对象）
- 范式（ReAct/Reflection/Plan-and-Solve/Multi-Agent）= 技术层的执行机制，
  通过策略钩子挂进本循环，按需组合启用

本包的 ReAct 循环是**四模式共用的底座**，不是 ask 模式的专属实现。

M15 追加：子代理（subagents/）+ 编排器（orchestrator.py）+ A2A 客户端（a2a.py）
——multi 模式由 M13 的"串行骨架"升级为真并行：Orchestrator 拆解任务、按写目标
静态分组、并发 spawn 独立上下文的子代理、聚合交付报告。
"""
from .a2a import A2AClient, A2AError, AgentCard, A2ATask
from .budgets import BudgetStatus, BudgetTracker, LoopDetector
from .dispatcher import DispatchConfig, Dispatcher
from .events import AgentEvent, EventBus
from .loop import AgentLoop, ContextBuilder, LoopConfig, LoopResult, Session
from .orchestrator import (DECOMPOSE_PROMPT, OrchestrResult, Orchestrator,
                           Subtask)
from .subagents import (Budget, SubagentSpec, SubtaskResult, spawn)

__all__ = [
    "BudgetStatus", "BudgetTracker", "LoopDetector",
    "DispatchConfig", "Dispatcher",
    "AgentEvent", "EventBus",
    "AgentLoop", "ContextBuilder", "LoopConfig", "LoopResult", "Session",
    # M15 子代理与编排
    "Budget", "SubagentSpec", "SubtaskResult", "spawn",
    "Orchestrator", "OrchestrResult", "Subtask", "DECOMPOSE_PROMPT",
    "A2AClient", "AgentCard", "A2ATask", "A2AError",
]
