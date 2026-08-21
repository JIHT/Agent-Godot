"""agent/events.py —— 统一事件出口（M03 §1.4 / §4 步骤 1）

电台广播制：Loop 不发 print、不写日志，只发结构化事件（AgentEvent）。
CLI 渲染成终端输出、Web（M20）序列化成 SSE、M17 轨迹录制器存成训练数据
——一个协议三个前端，M00"核心包纯库"铁律的输出侧兑现。

背压设计：asyncio.Queue(maxsize) 有上限——恶意长输出不会吃爆内存，
消费者跟不上时 emit 会等待（自然降速），而非无限堆积。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

STOP = object()  # 结束哨兵：stream() 消费到即停止


@dataclass
class AgentEvent:
    """一条结构化事件。type 是事件名，payload 是携带的数据。"""
    type: str
    payload: dict
    ts: float = field(default_factory=time.time)   # 多会话并发防串流的排序依据


class EventBus:
    """事件总线：emit 生产、stream 消费（异步迭代器）。

    用法（消费者）：
        async for ev in bus.stream():
            render(ev)
    """

    def __init__(self, maxsize: int = 1000):
        self._q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)

    async def emit(self, type_: str, **payload) -> None:
        """发一条事件。payload 用关键字参数，收进 event.payload。"""
        await self._q.put(AgentEvent(type_, payload))

    async def close(self) -> None:
        """广播结束：放入 STOP 哨兵，stream() 消费到即退出。"""
        await self._q.put(STOP)

    def stream(self) -> AsyncIterator[AgentEvent]:
        """事件流迭代器：消费到 STOP 哨兵自动停止。"""
        async def gen():
            while True:
                e = await self._q.get()
                if e is STOP:
                    break
                yield e  # type: ignore[misc]
        return gen()
