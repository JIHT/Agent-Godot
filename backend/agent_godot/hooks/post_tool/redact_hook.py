"""hooks/post_tool/redact_hook.py —— 工具结果脱敏（M14 §4 步骤 3）

为什么要脱敏：工具输出是给模型看的，但模型会把看到的东西复述给用户、写进
摘要（M07）、存进记忆（M08）、录进训练数据（M17）。一条 `token: sk-xxx`
一旦进记忆，就会在之后每一次会话里被召回——**扩散面比泄漏点大得多**。
所以脱敏放在 post_tool（出口），而不是在每个工具里各写一遍（横切关注分离）。

priority=90（安全类段位 50-99）：排在 format(100) 之前，与 §1.1 ②的
"脱敏 → 格式化"一致——格式化只管排版，不该看到密钥原文。
"""
from __future__ import annotations

import re
from dataclasses import replace

from agent_godot.tools import ToolResponse

from ..pipeline import HookContext, HookResult, HookSpec

# (正则, 替换)：命中即打码。顺序敏感——先精确后宽泛
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{12,}"), "sk-***"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)\b(ghp|gho|ghu|github_pat)_[A-Za-z0-9_]{10,}"), "ghp_***"),
    # key: value / key=value / "key": value（JSON）。`:=` 不匹配——那是 GDScript
    # 的类型推断声明（`var token := "x"`），脱敏它会把代码搅乱（误伤比漏网更烦）
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?key|secret[_-]?key|token|"
                r"password|passwd|secret)\b\s*[\"']?\s*(?::(?!=)|=)\s*"
                r"(\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)"), r"\1: ***"),
]


def redact_text(text: str) -> str:
    """对一段文本做脱敏（纯函数）。无命中返回原串（调用方据此判断有没有改）。"""
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_data(data):
    """递归脱敏结构化数据里的字符串值（data 是程序消费的，也会进 trace）。"""
    if isinstance(data, str):
        return redact_text(data)
    if isinstance(data, dict):
        return {k: redact_data(v) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_data(v) for v in data]
    return data


class RedactHook:
    """post_tool：把工具结果里的凭据打码（priority=90 安全类）。"""

    name = "redact"
    PRIORITY = 90

    @property
    def point(self) -> str:
        return "post_tool"

    def spec(self) -> HookSpec:
        return HookSpec(name=self.name, point="post_tool",
                        priority=self.PRIORITY, handler=self)

    async def __call__(self, ctx: HookContext) -> HookResult | None:
        resp = ctx.response
        if resp is None:
            return None
        summary = redact_text(resp.summary or "")
        data = redact_data(resp.data)
        if summary == (resp.summary or "") and _same(data, resp.data):
            return None                                   # 没有命中任何模式
        new_resp = replace(resp, summary=summary, data=data)
        return HookResult.modify(response=new_resp,
                                 reason=f"脱敏命中（{ctx.tool}）")


def _same(a, b) -> bool:
    """结构化数据等价判定（dict/list 递归；其余用 ==）。"""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


__all__ = ["RedactHook", "redact_text", "redact_data"]
