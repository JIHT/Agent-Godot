"""session/manager.py —— 事件溯源：创建/恢复/持久化（M09 §1.3 / §4 步骤 5）

Session 是有明确生命周期的状态机，**每个状态迁移点都是持久化点**——
事件 append 落 SQLite（每个迁移点的事后保障），恢复 = 全量事件确定性回放。
断线恢复三层里的 L2/L3 在这里落地：进程崩了从事件流重启循环；
隔天 /resume 纪要重建"昨天做到哪了"。事件流同时是 M17 GRPO 的训练数据
原料——生产可靠性设施与训练数据采集是同一套东西。

Session 与 agent.loop.Session 鸭类型兼容（session_id/messages/append），
可直接传给 AgentLoop.run——loop 每条 append 都被翻译成事件落盘。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agent_godot.core import Message, ToolCall
from agent_godot.tools.godot.checkpoints import TaskCheckpoints

from ..permission.confirm import ConfirmAnswer, PendingConfirm
from ..permission.risk import RiskLevel
from .state import (AssistantMsg, CompactDone, CompactStarted, ConfirmAnswered,
                    ConfirmAsked, InvalidTransition, Rewind, SessionClosed,
                    SessionCreated, SessionError, SessionEvent, SessionState,
                    SystemMsg, ToolDone, UserInput, event_from_dict)

if TYPE_CHECKING:
    from .rewind import RewindReport


def _new_session_id() -> str:
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


class Session:
    """事件溯源会话：状态 = 初始状态 + 事件序列回放。"""

    def __init__(self, session_id: str, project_id: str = ""):
        self.session_id = session_id
        self.project_id = project_id
        self.state = SessionState.ACTIVE
        self.events: list[SessionEvent] = []
        self.messages: list[Message] = []          # 回放重建的对话状态
        self.pending_confirm: PendingConfirm | None = None
        self.pending_answer: ConfirmAnswer | None = None
        self.rolled_back_turns = 0
        self._resume_future: asyncio.Future | None = None
        self._persist: Callable[[SessionEvent, int], None] | None = None

    # ---------- 事件入口（apply = 回放；record = apply + 落盘） ----------

    def apply(self, event: SessionEvent) -> None:
        """按事件类型更新内部状态；非法迁移抛 InvalidTransition。"""
        if isinstance(event, SessionCreated):
            self.project_id = event.project_id or self.project_id
        elif isinstance(event, UserInput):
            if self.state is SessionState.WAITING_CONFIRM:
                raise InvalidTransition("waiting_confirm 期间不接受新输入")
            if self.state in (SessionState.ERROR, SessionState.CLOSED):
                self.state = SessionState.ACTIVE      # /resume 续聊（读纪要重开）
            self.messages.append(Message(role="user", content=event.text))
        elif isinstance(event, AssistantMsg):
            if self.state is SessionState.WAITING_CONFIRM:
                raise InvalidTransition("挂起期间不应产生助手消息")
            calls = [ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                     for tc in (event.tool_calls or [])]
            self.messages.append(Message(role="assistant", content=event.content or None,
                                         tool_calls=calls or None))
        elif isinstance(event, ToolDone):
            self.messages.append(Message(role="tool", tool_call_id=event.call_id,
                                         content=event.summary))
        elif isinstance(event, SystemMsg):
            self.messages.append(Message(role="system", content=event.content))
        elif isinstance(event, ConfirmAsked):
            if self.state is not SessionState.ACTIVE:
                raise InvalidTransition(f"{self.state.value} 不能进入确认门")
            self.state = SessionState.WAITING_CONFIRM
            self.pending_confirm = PendingConfirm(
                call_id=event.call_id, tool=event.tool, args=dict(event.args),
                risk=RiskLevel(event.risk),
                preview=event.preview, expires_at=0.0, created_at=event.ts)
            self.pending_answer = None
        elif isinstance(event, ConfirmAnswered):
            if self.state is not SessionState.WAITING_CONFIRM:
                raise InvalidTransition(f"{self.state.value} 没有待确认的调用")
            self.state = SessionState.ACTIVE
            self.pending_answer = ConfirmAnswer(
                approved=event.approved, reason=event.reason,
                remember=event.remember)               # type: ignore[arg-type]
            self._settle_future()
        elif isinstance(event, CompactStarted):
            if self.state is not SessionState.ACTIVE:
                raise InvalidTransition("仅 active 可进入压缩")
            self.state = SessionState.COMPACTING
        elif isinstance(event, CompactDone):
            if self.state is not SessionState.COMPACTING:
                raise InvalidTransition("压缩完成事件没有对应开始事件")
            self.state = SessionState.ACTIVE
            self.messages.append(Message(role="system", content=event.summary))
        elif isinstance(event, SessionError):
            self.state = SessionState.ERROR
        elif isinstance(event, SessionClosed):
            self.state = SessionState.CLOSED
        elif isinstance(event, Rewind):
            # 截断回放已由 SessionManager.rewind 完成，此处仅记账
            self.state = SessionState.ROLLED_BACK      # 瞬态：进入后立即回 active
            self.state = SessionState.ACTIVE
            self.rolled_back_turns += event.turns
        else:
            raise ValueError(f"未知事件: {type(event).__name__}")
        self.events.append(event)

    def record(self, event: SessionEvent) -> None:
        """apply + 落盘（manager 注入 _persist 后每个迁移点都持久化）。"""
        self.apply(event)
        if self._persist is not None:
            self._persist(event, len(self.events))

    # ---------- Loop 兼容入口：普通 append 翻译成事件 ----------

    def append(self, msg: Message) -> None:
        """agent.loop.Session 鸭类型兼容——每条消息都进黑匣子。"""
        if msg.role == "user":
            self.record(UserInput(text=msg.content or ""))
        elif msg.role == "assistant":
            calls = [{"id": c.id, "name": c.name, "arguments": c.arguments}
                     for c in (msg.tool_calls or [])]
            self.record(AssistantMsg(content=msg.content or "",
                                     tool_calls=calls or None))
        elif msg.role == "tool":
            self.record(ToolDone(call_id=msg.tool_call_id or "",
                                 summary=msg.content or "", ok=True))
        else:
            self.record(SystemMsg(content=msg.content or ""))

    # ---------- 确认门挂起/恢复（§1.2） ----------

    async def suspend_with(self, pc: PendingConfirm) -> None:
        """状态机→waiting_confirm 并落盘（挂起后进程可退，状态全在盘上）。"""
        self.record(ConfirmAsked(call_id=pc.call_id, tool=pc.tool,
                                 args=dict(pc.args), risk=pc.risk.value,
                                 preview=pc.preview))

    async def answer(self, ans: ConfirmAnswer) -> None:
        """回填答案（CLI 输入线程 / Web REST 两路共用）：落盘并唤醒等待者。"""
        call_id = self.pending_confirm.call_id if self.pending_confirm else ""
        self.record(ConfirmAnswered(call_id=call_id, approved=ans.approved,
                                    reason=ans.reason, remember=ans.remember))

    async def wait_resume(self) -> ConfirmAnswer:
        """等待答案：已有答案（恢复回放）直接返回；否则挂 Future 等外部 set。"""
        if self.pending_answer is not None:
            return self.pending_answer
        if self._resume_future is None or self._resume_future.done():
            loop = asyncio.get_running_loop()
            self._resume_future = loop.create_future()
        return await self._resume_future

    def _settle_future(self) -> None:
        if (self._resume_future is not None
                and not self._resume_future.done()):
            self._resume_future.set_result(self.pending_answer)

    def events_since_suspend(self) -> list[SessionEvent]:
        """挂起点（最后一个 ConfirmAsked）之后的事件——恢复点重建的原料。"""
        idx = -1
        for i, e in enumerate(self.events):
            if isinstance(e, ConfirmAsked):
                idx = i
        return self.events[idx + 1:] if idx >= 0 else []

    def events_of_batch(self) -> list[SessionEvent]:
        """当前批调用的事件切片：最后一个带 tool_calls 的 AssistantMsg 之后。

        已完成调用的 ToolDone 发生在挂起**之前**（on_result 即时记账），
        恢复点重建"已完成响应表"要向前覆盖整个批次（§3）。
        """
        start = 0
        for i, e in enumerate(self.events):
            if isinstance(e, AssistantMsg) and e.tool_calls:
                start = i + 1
        return self.events[start:]

    def turns(self) -> int:
        """对话轮数（UserInput 计数）——rewind 的计量单位。"""
        return sum(1 for e in self.events if isinstance(e, UserInput))

    def snapshot_state(self) -> dict:
        """确定性对比测试用的状态快照（逐字段可比）。"""
        return {
            "session_id": self.session_id, "project_id": self.project_id,
            "state": self.state.value, "rolled_back_turns": self.rolled_back_turns,
            "messages": [(m.role, m.content, m.tool_call_id,
                          [(c.id, c.name, c.arguments) for c in (m.tool_calls or [])])
                         for m in self.messages],
            "pending_confirm": (None if self.pending_confirm is None else
                                (self.pending_confirm.call_id, self.pending_confirm.tool)),
            "pending_answer": (None if self.pending_answer is None else
                               (self.pending_answer.approved, self.pending_answer.reason)),
        }


class EventStore:
    """SQLite 事件仓库：append/load/truncate（M09 §4 步骤 5 persist_event）。"""

    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " session_id TEXT NOT NULL, seq INTEGER NOT NULL,"
            " type TEXT NOT NULL, payload TEXT NOT NULL, ts REAL NOT NULL,"
            " PRIMARY KEY (session_id, seq))")
        self._conn.commit()

    def append(self, session_id: str, seq: int, event: SessionEvent) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)",
            (session_id, seq, type(event).__name__,
             json.dumps(event.to_dict(), ensure_ascii=False), event.ts))
        self._conn.commit()

    def load(self, session_id: str) -> list[SessionEvent]:
        rows = self._conn.execute(
            "SELECT payload FROM events WHERE session_id=? ORDER BY seq",
            (session_id,)).fetchall()
        return [event_from_dict(json.loads(r[0])) for r in rows]

    def truncate(self, session_id: str, keep_seq: int) -> None:
        """物理截断：保留 seq <= keep_seq（rewind 丢掉最近 N 轮的事件）。"""
        self._conn.execute(
            "DELETE FROM events WHERE session_id=? AND seq>?",
            (session_id, keep_seq))
        self._conn.commit()

    def session_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT session_id, MAX(ts) FROM events GROUP BY session_id "
            "ORDER BY MAX(ts)").fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        self._conn.close()


class SessionManager:
    """会话生命周期管理：创建/恢复/答题/rewind/命名存档。"""

    def __init__(self, project_root: Path | None = None, *,
                 db_path: Path | None = None):
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.store = EventStore(db_path or self.project_root / ".agent_godot" / "sessions.db")
        # M06 联动：rewind 时按任务检查点回滚文件系统
        self.checkpoints = TaskCheckpoints(self.project_root)

    # ---------- 创建 / 恢复 ----------

    async def create(self, project_id: str = "") -> Session:
        s = Session(_new_session_id(), project_id)
        self._attach_persist(s)
        s.record(SessionCreated(project_id=project_id))
        return s

    async def resume(self, session_id: str) -> Session:
        """全量事件 → new Session → 逐条 apply（确定性回放，§1.3 ③）。"""
        events = self.store.load(session_id)
        if not events:
            raise KeyError(f"会话不存在: {session_id}")
        s = Session(session_id)
        for e in events:
            s.apply(e)
        self._attach_persist(s)
        return s

    async def resume_latest(self) -> Session:
        ids = self.store.session_ids()
        if not ids:
            raise KeyError("没有任何会话记录")
        return await self.resume(ids[-1])

    # ---------- 确认门答题（跨进程路径：Web REST / 重启后的 CLI） ----------

    async def answer_confirm(self, session_id: str, ans: ConfirmAnswer,
                             session: Session | None = None) -> Session:
        """恢复会话并回填答案（live session 直接答，不重复落盘入口）。"""
        s = session or await self.resume(session_id)
        if s.state is not SessionState.WAITING_CONFIRM:
            raise InvalidTransition(f"会话 {session_id} 不在等待确认（{s.state.value}）")
        await s.answer(ans)
        return s

    # ---------- rewind / 命名存档（委托 rewind.py） ----------

    async def rewind(self, session_id: str, turns: int) -> "RewindReport":
        from .rewind import rewind_to
        return await rewind_to(self, session_id, turns)

    async def checkpoint_named(self, session_id: str, name: str) -> list[str]:
        """/checkpoint save "重构前"：跨轮聚合当前全部任务检查点为命名存档。"""
        from .rewind import NamedCheckpoints
        task_ids = [i.task_id for i in self.checkpoints.list()]
        NamedCheckpoints(self.project_root).save(name, task_ids)
        return task_ids

    # ---------- 内部 ----------

    def _attach_persist(self, s: Session) -> None:
        def persist(event: SessionEvent, seq: int) -> None:
            self.store.append(s.session_id, seq, event)
        s._persist = persist
