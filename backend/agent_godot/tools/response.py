"""tools/response.py —— 工具结果协议（M04 §1.3 的最小版）

工具执行结果统一成 ToolResponse：成功/失败 + 内容/错误。
dispatcher 把异常也翻译成 ToolResponse(ok=False) 作为 Observation 回填——
模型读到错误可自己纠正（Agent"自我修复"的最小形态），而非循环崩溃。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolResponse:
    """一次工具执行的结果。"""
    call_id: str       # 对应 ToolCall.id，配对回填用
    ok: bool
    data: str = ""     # 成功时的输出
    error: str = ""    # 失败时的错误描述（给模型看的"为什么失败"）

    def render(self) -> str:
        """转成回填给模型的 tool 消息 content。"""
        if self.ok:
            return self.data
        return f"[工具执行失败] {self.error}"
