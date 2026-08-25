"""context：上下文工程（M07）—— 计数/滚动/截断/压缩/总装 四件套+压缩。

书桌管理：每轮开始前把上下文整理成"此刻最该看的东西"——
看完的归档进抽屉（摘要压缩），重要的钉在软木板（pin），最新的留在手边。
"""
from .builder import (BudgetConfig, ContextBuilder, ContextOverflowError,
                      HistoryConfig, Partition)
from .compressor import Compressor
from .history import HistoryConfig, HistoryManager
from .token_counter import TokenCounter
from .truncator import ObservationTruncator

__all__ = [
    "BudgetConfig", "ContextBuilder", "ContextOverflowError",
    "HistoryConfig", "Partition",
    "Compressor",
    "HistoryManager",
    "TokenCounter",
    "ObservationTruncator",
]
