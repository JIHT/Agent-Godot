"""session：会话系统（M09 后半）—— 六态状态机、事件溯源、断线恢复、rewind。

事件溯源（飞机黑匣子）：状态 = 初始状态 + 事件序列回放。消息、工具结果、
确认答案、rewind 全部以事件落 SQLite；恢复 = 确定性重放。事件流同时是
M17 GRPO 的训练数据原料——可靠性与数据采集是同一套东西。
"""
from .manager import EventStore, Session, SessionManager
from .rewind import NamedCheckpoints, RewindReport, rewind_to, split_before
from .state import (InvalidTransition, SessionEvent, SessionState,
                    event_from_dict)

__all__ = [
    "Session", "SessionManager", "EventStore",
    "SessionState", "SessionEvent", "InvalidTransition", "event_from_dict",
    "rewind_to", "split_before", "RewindReport", "NamedCheckpoints",
]
