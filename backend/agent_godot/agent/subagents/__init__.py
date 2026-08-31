"""agent/subagents —— 子代理包（M15）

三件套：
- base   ：SubagentSpec（角色合同）+ spawn（派生/隔离/销毁）+ SubtaskResult
- builtin：内置三角色（explorer/coder/verifier）+ 自定义角色 markdown 加载

★ 子代理的三条铁律（M15 §1.2）：
① 独立上下文：spawn 新建 Session，不挂主控历史（双向隔离）
② 工具白名单：registry.filter 产出的**视图**（ask 子代理物理上无写工具）
③ 只交报告：返回 SubtaskResult（报告+产出物+usage），过程上下文随 Session 销毁

★ 任务书自包含（§1.6）：子代理看不到主控对话，主控"当然知道"的约定漏进不了
任务书就会被"行业惯例"静默补全。三招闭环：CONSTRAINTS 无条件注入（治本）+
自报假设（让遗漏自己浮出来）+ 聚合侧约定比对。
"""
from .base import (CONSTRAINTS_RELPATH, DELIVERY_SPEC, Budget, Constraints,
                   Rule, SubagentSpec, SubtaskResult, WhitelistStrategy,
                   load_constraints, spawn)
from .builtin import (BUILTIN_ROLES, CODER, EXPLORER, VERIFIER,
                      RoleTemplate, build_all_specs, build_default_specs,
                      default_agent_roots, describe_specs, load_custom_specs)

__all__ = [
    "Budget", "SubagentSpec", "SubtaskResult", "WhitelistStrategy", "spawn",
    "Constraints", "Rule", "load_constraints", "DELIVERY_SPEC",
    "CONSTRAINTS_RELPATH",
    "BUILTIN_ROLES", "CODER", "EXPLORER", "VERIFIER", "RoleTemplate",
    "build_all_specs", "build_default_specs", "default_agent_roots",
    "describe_specs", "load_custom_specs",
]
