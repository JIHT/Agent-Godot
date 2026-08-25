"""permission：权限系统（M09 前半）—— 风险分级、规则引擎、决策门、确认门。

小区门禁三级：取快递自提（LOW 放行）/ 刷工卡进健身房（MEDIUM 规则放行）/
开保险柜必须本人到场（HIGH 确认门）。求值链：deny 全表 → 精确路径 →
通配规则 → defaults[risk]，首个命中即返回。
"""
from .confirm import ConfirmAnswer, ConfirmGate, PendingConfirm, denied_response, resume_batch
from .gate import GateDecision, PermissionGate
from .risk import RiskLevel, assess
from .rules import Decision, PermissionRule, RuleEngine, match_glob

__all__ = [
    "RiskLevel", "assess",
    "PermissionRule", "Decision", "RuleEngine", "match_glob",
    "GateDecision", "PermissionGate",
    "ConfirmAnswer", "ConfirmGate", "PendingConfirm",
    "denied_response", "resume_batch",
]
