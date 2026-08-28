"""hooks/pre_tool/permission_hook.py —— M09 权限门的 hook 化（M14 §4 步骤 2）

特例先落地，通用化后再收编（§7 问答 9）：M09 的 PermissionGate 先在
Dispatcher 里硬编码，M14 提供管线后把它包装成 priority=0 的 pre_tool hook
——权限检查由此从"内置特权"变成"可被替换/禁用/排序的普通插件"：
测试环境 `pipeline.unregister("permission")` 一行关门禁。

三分支翻译（gate 决策 → hook 协议）：
- allow        → None（pass，工具照常执行）
- deny         → veto（Dispatcher 翻译成 DENIED Observation）
- need_confirm → 由 hook 内驱动确认门（可能挂起等人签字），把门禁产出的
                 ToolResponse 用 veto 带回（★ 不带回就会执行两次）

★ need_confirm 走 veto 携带响应而非 pass：确认门内部（execute_now）已经
执行过工具，若返回 pass 让 Dispatcher 再执行一次 = 副作用重放（M09 §3 铁律）。
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent_godot.core import ToolCall

from ..pipeline import HookContext, HookResult, HookSpec

if TYPE_CHECKING:
    from agent_godot.permission.confirm import ConfirmGate


class PermissionHook:
    """把 PermissionGate / ConfirmGate 包装成 pre_tool hook（priority=0）。"""

    name = "permission"
    PRIORITY = 0                 # 系统级：永远最先跑（§7 问答 3 段位约定）

    def __init__(self, gate: "ConfirmGate", dispatcher=None):
        self.gate = gate
        self.dispatcher = dispatcher       # 确认门批准后现场执行的执行器

    @property
    def point(self) -> str:
        return "pre_tool"

    def spec(self) -> HookSpec:
        return HookSpec(name=self.name, point="pre_tool",
                        priority=self.PRIORITY, handler=self)

    async def __call__(self, ctx: HookContext) -> HookResult | None:
        call = ToolCall(id=ctx.call_id, name=ctx.tool,
                        arguments=json.dumps(ctx.args, ensure_ascii=False))
        decision = await self.gate.check(call)
        if decision.action == "allow":
            return None                                        # pass
        if decision.action == "deny":
            return HookResult.veto(decision.reason or "权限规则拒绝")

        # need_confirm：确认门自己会挂起等人（同进程 prompter / 跨进程 Future）
        request = getattr(self.gate, "request", None)
        if request is None or self.dispatcher is None:
            # 只有判定门没有确认门：不擅自执行，按"需用户确认"短路
            return HookResult.veto(
                decision.reason or "需用户确认（当前未挂载确认门）")
        resp = await request(call)
        # reported=True：确认门内部已 report_result（批准→execute_now、
        # 拒绝→denied_response），Dispatcher 收到后不得重复记账
        return HookResult.veto(
            decision.reason or "需用户确认（已由确认门处理）",
            response=resp, reported=True)


__all__ = ["PermissionHook"]
