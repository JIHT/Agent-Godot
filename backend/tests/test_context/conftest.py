"""tests/test_context/conftest.py —— M07 测试夹具：造会话 + 剧本式摘要假模型。"""
from __future__ import annotations

from agent_godot.core import LLMResponse, Message, ToolCall


class FakeSummaryLLM:
    """C 档压缩用的假模型：只实现 complete，返回预置纪要文本。"""

    def __init__(self, content: str = "目标: 加敌人 | 决策: 用 CharacterBody2D "
                                      "| 已改: enemy.gd(a3f2) | 未决: 音效 | 偏好: tabs 缩进"):
        self.content = content
        self.calls = 0

    async def complete(self, req) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.content, tool_calls=[],
                           usage=None, finish_reason="stop")


class ExplodingLLM:
    """必炸模型：验证 C 档失败自动降级 B 档。"""

    async def complete(self, req) -> LLMResponse:
        raise RuntimeError("summarize backend down")


def make_round(messages: list[Message], i: int, obs: str = "ok",
               user: str | None = None) -> None:
    """追加一个标准工具轮：[user?] assistant(tool_calls) → tool。

    所有消息直接 append 到传入列表（对象身份稳定，pin 按 id 生效）。
    """
    if user is not None:
        messages.append(Message(role="user", content=user))
    messages.append(Message(
        role="assistant", tool_calls=[ToolCall(id=f"c{i}", name="echo",
                                               arguments='{"x": 1}')]))
    messages.append(Message(role="tool", tool_call_id=f"c{i}", content=obs))


def make_session_messages(turns: int = 50, obs_chars: int = 60) -> list[Message]:
    """造 turns 轮混合会话（含肥 Observation）。"""
    msgs: list[Message] = [Message(role="system", content="你是 Godot 游戏 Agent。")]
    msgs.append(Message(role="user", content="帮我加一个敌人"))
    for i in range(turns):
        make_round(msgs, i, obs="观察结果 " * (obs_chars // 5),
                   user=f"第 {i} 轮指令" if i % 5 == 0 else None)
    msgs.append(Message(role="user", content="现在总结一下"))
    return msgs
