"""tests/test_agent/conftest.py —— 测试夹具：剧本式 FakeLLM + 事件构造器。

FakeLLM 实现 M02 的 LLM Protocol（只实现 stream，loop 只用 stream）——
"不继承任何人、只认形状"的 Protocol 红利在测试里兑现：不用起真实模型。
"""
from __future__ import annotations

from agent_godot.core import StreamEvent, ToolCall, Usage
from agent_godot.agent.dispatcher import Dispatcher
from agent_godot.tools import ToolRegistry


def text_ev(text: str) -> StreamEvent:
    return StreamEvent(type="text_delta", text=text)


def done_ev(calls: list[ToolCall] | None = None,
            finish: str = "stop") -> StreamEvent:
    return StreamEvent(type="done", tool_calls=calls, finish_reason=finish)


def usage_ev(inp: int = 10, out: int = 5) -> StreamEvent:
    return StreamEvent(type="usage", usage=Usage(inp, out))


class FakeLLM:
    """剧本式假模型：按轮次返回预置的事件序列。

    script: list[list[StreamEvent]]——第 i 轮调用 stream 返回第 i 个列表
    （越界则循环用最后一段，保证"永远调工具"的假模型不会提前耗尽）。
    """

    def __init__(self, script: list[list[StreamEvent]]):
        self.script = script
        self.calls = 0

    async def stream(self, req):
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for ev in self.script[idx]:
            yield ev


def make_dispatcher() -> Dispatcher:
    """构造带一个只读 echo 工具的 Dispatcher。"""
    reg = ToolRegistry()

    async def echo(x: str = "ok") -> str:
        return x

    reg.register(name="echo", description="回显",
                 parameters={"type": "object",
                             "properties": {"x": {"type": "string"}},
                             "required": []}, readonly=True)(echo)
    return Dispatcher(reg)
