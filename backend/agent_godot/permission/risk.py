"""permission/risk.py —— 三级风险枚举 + 评级三问（M09 §1.1）

小区门禁三级：LOW=快递柜自提（默认放行）/ MEDIUM=刷工卡进健身房（规则决定）/
HIGH=开保险柜必须本人到场（默认询问）。任何新工具过"评级三问"：
可逆吗（有检查点=可逆，降一级）/ 影响范围（项目内→系统级→网络逐级升）/
频率（高频询问=确认疲劳，要规则化放行）。

M06 深层耦合：检查点把"文件写错"的损失从"人工恢复数小时"压到"rewind 秒级"
——损失项坍缩后整体风险降档，所以"写且无检查点"要升回 HIGH。
"""
from __future__ import annotations

from enum import Enum


class RiskLevel(Enum):
    LOW = "low"          # 只读无副作用：list/read/search/check → 默认放行
    MEDIUM = "medium"    # 本地可逆写：write_file/edit_scene → 规则决定
    HIGH = "high"        # 不可逆或出边界：delete/网络/任意命令 → 默认询问


def assess(tool_meta, has_checkpoint: bool = True) -> RiskLevel:
    """评级三问代码化（M04 ToolMeta 静态声明 × M06 可恢复性动态修正）。

    - 只读 → LOW（无副作用，问都不用问）
    - 写 + 检查点在 → MEDIUM（错了能 rewind，损失可控）
    - 写 + 无检查点 / 声明 HIGH（网络/任意命令/删除）→ HIGH
    - 声明 LOW 但会写（注册失误兜底）→ 至少 MEDIUM
    """
    if getattr(tool_meta, "readonly", True):
        return RiskLevel.LOW
    declared = RiskLevel(getattr(tool_meta, "risk", "medium"))
    if declared is RiskLevel.HIGH:
        return RiskLevel.HIGH
    if declared is RiskLevel.LOW:
        return RiskLevel.MEDIUM
    return RiskLevel.MEDIUM if has_checkpoint else RiskLevel.HIGH
