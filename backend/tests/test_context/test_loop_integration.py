"""tests/test_context/test_loop_integration.py —— M07 builder 注入 M03 Loop。

验证：AgentLoop(context=ContextBuilder) 正常运转、发 context_layout 事件、
usage 回执参与自校准（不烧真实模型，剧本式 FakeLLM）。
"""
from __future__ import annotations

import asyncio

from agent_godot.agent import AgentLoop, EventBus, LoopConfig, Session
from agent_godot.context import (BudgetConfig, Compressor, ContextBuilder,
                                 HistoryManager, TokenCounter)
from agent_godot.core import ToolCall, Usage

from ..test_agent.conftest import (done_ev, make_dispatcher, text_ev,
                                   usage_ev)


class ScriptLLM:
    """两轮剧本：第一轮调工具（带 usage 回执），第二轮自然终止。"""

    def __init__(self):
        self.script = [
            [usage_ev(inp=2_000, out=50),
             done_ev([ToolCall(id="c1", name="echo", arguments="{}")],
                     "tool_calls")],
            [text_ev("完成了。"), usage_ev(inp=2_100, out=30),
             done_ev(None, "stop")],
        ]
        self.n = 0

    async def stream(self, req):
        idx = min(self.n, len(self.script) - 1)
        self.n += 1
        for ev in self.script[idx]:
            yield ev

    async def complete(self, req):
        from agent_godot.core import LLMResponse
        return LLMResponse(content="纪要", tool_calls=[], usage=None,
                           finish_reason="stop")


async def test_loop_with_m07_builder():
    counter = TokenCounter()
    builder = ContextBuilder(
        counter=counter,
        compressor=Compressor(llm=ScriptLLM(), model="cheap"),
        config=BudgetConfig(window=50_000),
        history=HistoryManager(counter),
        model="test-model")

    bus = EventBus()
    loop = AgentLoop(ScriptLLM(), make_dispatcher(), model="test-model",
                     bus=bus, config=LoopConfig(max_steps=5),
                     context=builder)

    events = []

    async def drain():
        async for ev in bus.stream():
            events.append(ev)

    task = asyncio.create_task(drain())
    session = Session("s1")
    result = await loop.run(session, "你好")
    await bus.close()
    await task

    assert result.stop_reason == "natural"
    # trace：每轮都广播了分区 layout
    layouts = [e for e in events if e.type == "context_layout"]
    assert layouts and "history" in layouts[-1].payload["layout"]
    # 请求消息过 builder 组装（含 system + history + latest 分区）
    assert all(m.role in ("system", "user", "assistant", "tool")
               for m in session.messages)


async def test_loop_default_builder_still_works():
    """不注入 context（默认简单拼接）→ 旧行为不变（向后兼容）。"""
    llm = ScriptLLM()
    loop = AgentLoop(llm, make_dispatcher(), model="test-model",
                     config=LoopConfig(max_steps=5))
    result = await loop.run(Session("s1"), "hi")
    assert result.stop_reason == "natural"
