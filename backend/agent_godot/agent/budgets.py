"""agent/budgets.py —— 四维预算 + 死循环检测（M03 §1.2 / §4 步骤 2）

给发动机装"仪表盘和断油系统"：
- BudgetTracker：steps/tokens/usd/wall_time 四维，任一触顶即"优雅收尾"
  （靠边停车总结，而非抛异常甩飞乘客）
- LoopDetector：死循环指纹（tool_name + args 的元组），滑窗内重复 ≥N 判定——
  比"步数上限"精细，能在浪费 10 步前拦住
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from agent_godot.core import ToolCall, Usage


@dataclass
class BudgetStatus:
    """预算检查结果。exhausted=True 时 reason 标明哪个维度触顶。"""
    exhausted: bool
    reason: str | None = None   # max_steps / token_budget / usd_budget / timeout


class BudgetTracker:
    """四维预算记账器。

    check() 必须在每轮开始调用（而非工具执行后）——工具本身可能跑 5 分钟，
    检查晚了等于没检查（M03 §1.2 易错点①）。
    """

    def __init__(self, *, max_steps: int = 25, token_budget: int = 200_000,
                 usd_budget: float = 0.5, wall_time_budget: float = 600.0):
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.usd_budget = usd_budget
        self.wall_time_budget = wall_time_budget
        self.reset()

    def reset(self) -> None:
        self.steps = 0
        self.tokens = 0
        self.cost = 0.0
        self._start = time.monotonic()   # ★ 单调钟：墙钟被 NTP 回拨会"凭空续命"

    def check(self) -> BudgetStatus:
        """四维逐一比对，任一触顶返回 exhausted。"""
        if self.steps >= self.max_steps:
            return BudgetStatus(True, "max_steps")
        if self.tokens >= self.token_budget:
            return BudgetStatus(True, "token_budget")
        if self.cost >= self.usd_budget:
            return BudgetStatus(True, "usd_budget")
        if time.monotonic() - self._start >= self.wall_time_budget:
            return BudgetStatus(True, "timeout")
        return BudgetStatus(False)

    def record_usage(self, usage: Usage) -> None:
        """吃 M02 的电表读数：token 维度累加输入+输出，usd 维度累加成本。"""
        self.tokens += usage.input_tokens + usage.output_tokens
        self.cost += usage.cost_usd

    def record_step(self) -> None:
        self.steps += 1


class LoopDetector:
    """死循环检测：滑动窗口内相同调用指纹重复 ≥max_repeat 判定。

    指纹用 tuple（而非 hash）——hash 有碰撞风险且 Python 进程间随机化，
    tuple 比较既确定性又零碰撞。只拦"同参数真重复"，不误伤"参数在变的合理重试"。
    """

    def __init__(self, window: int = 3, max_repeat: int = 3):
        self.window = window
        self.max_repeat = max_repeat
        self.history: deque[tuple] = deque(maxlen=window)

    def check(self, calls: list[ToolCall]) -> bool:
        """返回 True 表示检测到死循环（本轮调用指纹在窗口内已重复达阈值）。"""
        fp = tuple(sorted((c.name, c.arguments) for c in calls))
        self.history.append(fp)
        return self.history.count(fp) >= self.max_repeat
