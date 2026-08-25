"""tests/test_agent/test_loop.py —— M03 §5 的四个验收单测。

用 FakeLLM + 剧本，不烧真实模型，验证循环的四个工程能力：
max_steps 熔断 / 死循环干预 / 工具错误变 Observation / 事件顺序。
"""
from __future__ import annotations

import asyncio

from agent_godot.agent import AgentLoop, EventBus, LoopConfig, Session
from agent_godot.agent.dispatcher import Dispatcher
from agent_godot.core import ToolCall
from agent_godot.tools import ToolRegistry

from .conftest import FakeLLM, done_ev, make_dispatcher, text_ev


def _loop(llm, dispatcher, **kw):
    return AgentLoop(llm, dispatcher, model="test-model", **kw)


async def test_loop_stops_on_max_steps():
    """max_steps 触顶 → 优雅收尾，stop_reason='max_steps' 且 steps 精确。

    注意：用"每轮不同参数"的调用——若用相同调用，连续 3 次会先触发死循环
    检测（loop_detected）抢在步数熔断之前，两个关注点必须隔离测试。
    """

    class VaryingLLM:
        def __init__(self):
            self.n = 0

        async def stream(self, req):
            self.n += 1
            yield done_ev([ToolCall(id=f"c{self.n}", name="echo",
                                    arguments=f'{{"x": "{self.n}"}}')],
                          "tool_calls")

    loop = _loop(VaryingLLM(), make_dispatcher(), config=LoopConfig(max_steps=3))
    result = await loop.run(Session("s1"), "go")
    assert result.stop_reason == "max_steps"
    assert result.steps == 3


async def test_loop_detector_intervenes():
    """连续 3 次相同调用 → 第一次劝导 → 仍重复 → 硬停 loop_detected。"""
    llm = FakeLLM([[done_ev([ToolCall(id="c1", name="echo", arguments="{}")],
                            "tool_calls")]])
    loop = _loop(llm, make_dispatcher())
    result = await loop.run(Session("s1"), "go")
    assert result.stop_reason == "loop_detected"


async def test_tool_error_becomes_observation():
    """工具抛异常 → 循环不中断，错误作为 ok=False 的 tool 消息回填，最终自然终止。"""
    from .conftest import BoomTool, _attach_meta

    reg = ToolRegistry()
    reg.register(_attach_meta(BoomTool, "boom")())

    script = [
        [done_ev([ToolCall(id="c1", name="boom", arguments="{}")], "tool_calls")],
        [text_ev("工具失败了，但我已经总结好。"), done_ev(None, "stop")],
    ]
    loop = _loop(FakeLLM(script), Dispatcher(reg))
    session = Session("s1")
    result = await loop.run(session, "go")
    assert result.stop_reason == "natural"
    tool_msgs = [m.content for m in session.messages if m.role == "tool"]
    assert any("boom" in c and "internal" in c for c in tool_msgs)


async def test_events_ordered():
    """tool_call_result 事件必须晚于对应的 tool_call_start。"""
    script = [
        [done_ev([ToolCall(id="c1", name="echo", arguments="{}")], "tool_calls")],
        [text_ev("done"), done_ev(None, "stop")],
    ]
    bus = EventBus()
    loop = _loop(FakeLLM(script), make_dispatcher(), bus=bus)
    session = Session("s1")

    events = []

    async def drain():
        async for ev in bus.stream():
            events.append(ev)

    task = asyncio.create_task(drain())
    await loop.run(session, "go")
    await bus.close()
    await task

    start_ts = next(e.ts for e in events if e.type == "tool_call_start")
    result_ts = next(e.ts for e in events if e.type == "tool_call_result")
    assert result_ts >= start_ts
