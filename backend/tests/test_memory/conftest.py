"""tests/test_memory/conftest.py —— M08 测试夹具：假 LLM + 内存 Store。"""
from __future__ import annotations

import time

from agent_godot.core import LLMResponse, Message
from agent_godot.memory import MemoryStore, fake_embed


class FakeExtractLLM:
    """抽取用假模型：按调用序号返回预置 JSON。

    scripts[0] = 抽取结果（JSON 数组字符串）
    scripts[1+] = 四路决策结果（JSON 字符串）
    越界循环用最后一段。
    """

    def __init__(self, scripts: list[str]):
        self.scripts = scripts
        self.calls = 0
        self.call_contents: list[str] = []

    async def complete(self, req) -> LLMResponse:
        idx = min(self.calls, len(self.scripts) - 1)
        self.calls += 1
        text = self.scripts[idx]
        # 记录第一次调用（抽取）的 prompt 内容用于断言
        if req.messages and req.messages[0].content:
            self.call_contents.append(req.messages[0].content[:200])
        return LLMResponse(content=text, tool_calls=[],
                           usage=None, finish_reason="stop")


class EchoLLM:
    """把 prompt 原样返回的假模型（调试用）。"""

    async def complete(self, req) -> LLMResponse:
        text = req.messages[0].content if req.messages else ""
        return LLMResponse(content=text, tool_calls=[],
                           usage=None, finish_reason="stop")


def make_store() -> MemoryStore:
    """内存 SQLite Store + 假嵌入。"""
    return MemoryStore(db_path=":memory:", embedder=fake_embed)


def make_session_messages() -> list[Message]:
    """造一个含约定 + 踩坑 + 寒暄的会话。"""
    return [
        Message(role="system", content="你是 Godot 游戏 Agent。"),
        Message(role="user", content="你好，开始干活吧"),        # 寒暄（应被丢弃）
        Message(role="user", content="以后信号回调都叫 _on_x_y"),
        Message(role="assistant", content="好的，记住了。"),
        Message(role="user", content="给敌人加 AI 巡逻"),
        Message(role="assistant", content="CharacterBody2D 的 contacts_reported "
                                          "在 Godot 4.3 改名成 max_contacts_reported 了，踩坑了。"),
        Message(role="user", content="谢谢"),
    ]
