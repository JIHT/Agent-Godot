"""session/state.py —— 六态状态机 + 事件定义（M09 §1.3 / §4 步骤 4）

飞机黑匣子：不存"每分钟一张状态快照"，把每个动作（用户输入/助手消息/
工具结果/确认答案/rewind）都记成事件；还原任意时刻状态 = 从头回放。
回放必须**确定性**——事件存"事实结果"不存"生成过程"：时间戳在产生时
固化存值、随机结果直接存、外部响应存快照（§1.3 易错点①）。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum


class SessionState(Enum):
    ACTIVE = "active"
    WAITING_CONFIRM = "waiting_confirm"    # 确认门挂起，等人签字
    COMPACTING = "compacting"              # 上下文压缩中（M07）
    ERROR = "error"                        # 不可恢复错误
    ROLLED_BACK = "rolled_back"            # /rewind 瞬态（进入后立即回 active）
    CLOSED = "closed"                      # 正常结束


class InvalidTransition(Exception):
    """非法状态迁移（如 waiting_confirm 期间来新 UserInput）——状态机纪律。"""


# ---------- 事件家族（不可变的历史事实） ----------

@dataclass
class SessionEvent:
    """事件基类：ts 在产生时固化（回放确定性纪律）。"""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = type(self).__name__
        return d


@dataclass
class SessionCreated(SessionEvent):
    project_id: str = ""


@dataclass
class UserInput(SessionEvent):
    text: str = ""


@dataclass
class AssistantMsg(SessionEvent):
    content: str = ""
    tool_calls: list[dict] | None = None    # [{id,name,arguments}]（事实快照）


@dataclass
class ToolDone(SessionEvent):
    call_id: str = ""
    tool: str = ""
    ok: bool = True
    summary: str = ""


@dataclass
class SystemMsg(SessionEvent):
    content: str = ""


@dataclass
class ConfirmAsked(SessionEvent):
    call_id: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)
    risk: str = "high"
    preview: str | None = None


@dataclass
class ConfirmAnswered(SessionEvent):
    call_id: str = ""
    approved: bool = False
    reason: str = ""
    remember: str = "never"


@dataclass
class Rewind(SessionEvent):
    turns: int = 0
    files: list[str] = field(default_factory=list)     # 联动回滚的文件清单
    task_ids: list[str] = field(default_factory=list)  # 回滚的任务检查点


@dataclass
class CompactStarted(SessionEvent):
    pass


@dataclass
class CompactDone(SessionEvent):
    summary: str = ""


@dataclass
class SessionError(SessionEvent):
    message: str = ""


@dataclass
class SessionClosed(SessionEvent):
    final_text: str = ""


_EVENT_TYPES: dict[str, type[SessionEvent]] = {
    t.__name__: t for t in (
        SessionCreated, UserInput, AssistantMsg, ToolDone, SystemMsg,
        ConfirmAsked, ConfirmAnswered, Rewind, CompactStarted, CompactDone,
        SessionError, SessionClosed)}


def event_from_dict(d: dict) -> SessionEvent:
    """反序列化：type 字段路由到对应事件类（未知类型报错，不静默丢弃）。"""
    payload = {k: v for k, v in d.items() if k != "type"}
    cls = _EVENT_TYPES.get(d.get("type", ""))
    if cls is None:
        raise ValueError(f"未知事件类型: {d.get('type')!r}")
    return cls(**payload)
