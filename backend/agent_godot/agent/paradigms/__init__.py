"""agent/paradigms —— 四模式策略包（M13）

换挡器总成：同一 ReAct 循环（M03）挂四套"契约配置"对象（§1.4）——
ask 顾问（只读）/ craft 执行者（全工具+自检）/ plan 架构师（DAG+审批）
/ multi 车队（并行派发）。

★ 模式层 ⊥ 范式层（两层正交，不可绑定，见 M13 §1.3）：
- 模式（本包）= 产品层的"人机协作契约"：模型能做什么、要不要问人
- 范式 = 技术层的"执行机制"：ReAct/Reflection/Plan-and-Solve/Multi-Agent，
  在各模式下按需组合启用，由实际问题决定

所以本包名虽叫 paradigms，装的其实是 modes 的策略对象——范式是通过
策略钩子（on_tool_done / before_loop / should_continue）挂进来的能力，
不等于模式本身。例如 plan 模式每个 DAG 节点会调 loop.run(mode="craft")，
一次 plan 执行就同时用到 Plan-and-Solve + ReAct + Reflection 三个范式。

import 本包即触发四个策略类的注册（副作用），PARADIGMS 里立即可查。
"""
from __future__ import annotations

from .base import (ModeConfig, ModeStrategy, PARADIGMS, get_strategy,
                   register)
from .ask import AskStrategy
from .craft import CraftStrategy, VerifyLoop
from .multi import MultiStrategy
from .plan import (PlanApprover, PlanCycleError, PlanGraph, PlanNode,
                   PlanStrategy)

__all__ = [
    "ModeConfig", "ModeStrategy", "PARADIGMS", "get_strategy", "register",
    "AskStrategy",
    "CraftStrategy", "VerifyLoop",
    "MultiStrategy",
    "PlanApprover", "PlanCycleError", "PlanGraph", "PlanNode", "PlanStrategy",
]
