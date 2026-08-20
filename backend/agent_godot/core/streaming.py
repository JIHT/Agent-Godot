"""core/streaming.py —— SSE 解析 + 分片聚合（M02 §1.1 / §4 步骤 3）

两个角色：
- parse_sse_line：剥协议外壳（"data: " 前缀），一行 JSON 进一个 dict 出
- StreamAggregator：拼图工人——分片到达的 tool_call 聚成完整 ToolCall
  （id/name 首帧到、arguments 字符串按块拼、finish_reason 时收口）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # 只在类型检查时导入：避免与 llm.py 循环依赖
    from .llm import ToolCall, Usage


DONE = object()  # [DONE] 帧哨兵：比返回 None 安全（None 与空行会混淆）


def parse_sse_line(line: str) -> dict | object | None:
    """解析 SSE 的一行。返回 dict=正常消息 / DONE=流结束 / None=空行心跳。"""
    if not line.startswith("data: "):
        return None
    payload = line[len("data: "):]
    if payload.strip() == "[DONE]":
        return DONE
    return json.loads(payload)


@dataclass
class StreamEvent:
    """统一流事件（方言已翻译完毕）：M03 的 AgentLoop 只消费这个。

    一个对象承载五种类型（type 字段区分），未用到的字段为 None。
    """
    type: Literal["text_delta", "tool_call_delta", "usage", "done", "error"]
    text: str | None = None              # text_delta：增量文本（含思考流）
    tool_calls: list | None = None       # done 帧：聚合完成的完整调用列表
    usage: "Usage | None" = None         # usage 帧：末帧账单
    finish_reason: str | None = None     # done 帧：stop / tool_calls / length
    error: str | None = None             # error 帧


@dataclass
class _Buffer:
    """一个 tool_call 的拼图缓冲：多调用并行时按 index 分桶各拼各的。"""
    id: str = ""
    name: str = ""
    args_parts: list[str] = field(default_factory=list)


class StreamAggregator:
    """拼图工人。用法：每行 SSE → parse_sse_line → feed(dict) → 收事件列表。"""

    def __init__(self) -> None:
        self._buffers: dict[int, _Buffer] = {}
        self.text: str = ""
        self.tool_calls: list = []          # list[ToolCall]
        self.usage = None                   # Usage | None
        self.finish_reason: str | None = None

    def feed(self, chunk: dict) -> list[StreamEvent]:
        events: list[StreamEvent] = []

        # ① usage 末帧：choices 为空数组（include_usage 的产物，必须判空！）
        choices = chunk.get("choices") or []
        if not choices:
            if usage := chunk.get("usage"):
                from .llm import Usage  # 局部导入：打破模块级循环
                self.usage = Usage(
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    cost_usd=0.0)  # 成本按单价表在网关层算，这里先记账 token
                events.append(StreamEvent(type="usage", usage=self.usage))
            return events

        choice = choices[0]
        delta = choice.get("delta") or {}

        # ② 文本增量（含思考型模型的 reasoning_content——Qwen3 思考流同透传）
        if piece := delta.get("content") or delta.get("reasoning_content"):
            self.text += piece
            events.append(StreamEvent(type="text_delta", text=piece))

        # ③ 工具调用分片：{index, id?, function: {name?, arguments?}}
        for frag in delta.get("tool_calls") or []:
            idx = frag.get("index", 0)
            buf = self._buffers.setdefault(idx, _Buffer())
            if tc_id := frag.get("id"):
                buf.id = tc_id
            fn = frag.get("function") or {}
            if name := fn.get("name"):
                buf.name = name
            if args := fn.get("arguments"):
                buf.args_parts.append(args)  # arguments 字符串按块拼接
            events.append(StreamEvent(type="tool_call_delta"))

        # ④ 结束帧：finish_reason 到达 → 收口
        if reason := choice.get("finish_reason"):
            self.finish_reason = reason
            if reason == "tool_calls":
                self._finalize_tool_calls()
            events.append(StreamEvent(
                type="done", finish_reason=reason,
                tool_calls=self.tool_calls or None))
        return events

    def _finalize_tool_calls(self) -> None:
        """全部缓冲桶收口成完整 ToolCall（按 index 排序）。"""
        from .llm import ToolCall
        self.tool_calls = []
        for idx in sorted(self._buffers):
            buf = self._buffers[idx]
            raw = "".join(buf.args_parts)
            if raw:
                json.loads(raw)  # 校验：半个 JSON 会抛错=聚合有 bug
            self.tool_calls.append(
                ToolCall(id=buf.id or f"call_{idx}", name=buf.name,
                         arguments=raw))
