"""permission/gate.py —— 决策入口（M09 §4 步骤 2，挂在 Dispatcher 前）

门禁前台：拿工卡（ToolCall）→ 查手册（RuleEngine）→ 给出三选一：
allow（放行）/ deny（规则直接拒）/ need_confirm（送确认门）。
本类只做"判定"，不做"拦下等人"——挂起/恢复是 confirm.ConfirmGate 的事。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agent_godot.core import ToolCall

from .risk import RiskLevel, assess
from .rules import RuleEngine

if TYPE_CHECKING:
    from agent_godot.tools import ToolRegistry


@dataclass
class GateDecision:
    action: Literal["allow", "deny", "need_confirm"]
    reason: str = ""


class PermissionGate:
    """判定门：registry 拿 risk → rules.decide → 三分支翻译成 GateDecision。"""

    def __init__(self, rules: RuleEngine, session=None,
                 registry: "ToolRegistry | None" = None):
        self.rules = rules
        self.session = session          # M09 Session（记录审计事件用，可空）
        self.registry = registry

    async def check(self, call: ToolCall) -> GateDecision:
        # 模型幻觉工具名：直接拒（registry.has 校验，比执行后报错更早更安全）
        if self.registry is not None and not self.registry.has(call.name):
            return GateDecision("deny", f"未注册的工具: {call.name}")
        risk = self.risk_of(call.name)
        d = self.rules.decide(call.name, call.arguments, risk=risk.value)
        if d.action == "allow":
            return GateDecision("allow", d.reason)
        if d.action == "deny":
            return GateDecision("deny", d.reason)
        return GateDecision("need_confirm", d.reason)

    def risk_of(self, tool_name: str) -> RiskLevel:
        """评级：registry 有声明用声明（assess 叠加可恢复性），未知按 HIGH。"""
        if self.registry is not None and self.registry.has(tool_name):
            return assess(self.registry.spec(tool_name).meta)
        return RiskLevel.HIGH
