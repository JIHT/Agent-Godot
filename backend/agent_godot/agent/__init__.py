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

M15 第二轮加固（§1.4 / §1.6，不新增文件）：
- 冲突判定：`WriteScope` 归一化（分隔符 / 协议前缀 / 大小写跟随文件系统）+
  前缀树双向包含 + 访问三态（READ/WRITE/EXCLUSIVE），`write_targets` 是
  **三态**（未声明 ≠ 只读），判定后走七类处置决策树（escalate/serialize/
  depends/contract/merge）。
- 串行链：前驱产出注入（`_chain_ctx`）+ fail-fast（后继标 blocked）。
- 约定传递：CONSTRAINTS 无条件注入所有角色 + 自报假设结构化 + 聚合侧比对。
"""
from .a2a import A2AClient, A2AError, AgentCard, A2ATask
from .budgets import BudgetStatus, BudgetTracker, LoopDetector
from .dispatcher import DispatchConfig, Dispatcher
from .events import AgentEvent, EventBus
from .loop import AgentLoop, ContextBuilder, LoopConfig, LoopResult, Session
from .orchestrator import (DECOMPOSE_PROMPT, Access, Conflict, OrchestrResult,
                           Orchestrator, Subtask, WriteScope)
from .subagents import (Budget, Constraints, Rule, SubagentSpec, SubtaskResult,
                        load_constraints, spawn)

__all__ = [
    "BudgetStatus", "BudgetTracker", "LoopDetector",
    "DispatchConfig", "Dispatcher",
    "AgentEvent", "EventBus",
    "AgentLoop", "ContextBuilder", "LoopConfig", "LoopResult", "Session",
    # M15 子代理与编排
    "Budget", "SubagentSpec", "SubtaskResult", "spawn",
    "Orchestrator", "OrchestrResult", "Subtask", "DECOMPOSE_PROMPT",
    # M15 §1.4 冲突判定与处置
    "Access", "WriteScope", "Conflict",
    # M15 §1.6 约定传递
    "Constraints", "Rule", "load_constraints",
    # M15 §1.5 A2A
    "A2AClient", "AgentCard", "A2ATask", "A2AError",
]
