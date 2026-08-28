"""agent/paradigms/ask.py —— ask 模式：顾问契约（M13 §1.1 / §4 步骤 2）

契约档位：只答不改。config = readonly + 0.7，全部钩子用默认实现——
20 行胶水，验证"ask = M03 循环原样 + 只读契约"（回归不碎）。

写工具被 tools_view 物理过滤掉：模型拿到的工具列表里根本没有 write_file，
工具集即能力边界——不是靠 prompt 说"请不要改文件"（君子协定），而是
物理上给不出写工具。

★ 范式说明（M13 §1.3）：ask 内部照样在跑 ReAct（多轮检索：搜 → 看结果
→ 决定再搜什么 → 组织回答），只是手上没了写工具。Reflection 在此模式下
不适用（无写产物可供客观校验——回答文案好坏没有客观验证器）。
"""
from __future__ import annotations

from .base import ModeConfig, ModeStrategy, register


@register
class AskStrategy(ModeStrategy):
    mode = "ask"
    config = ModeConfig(tools="readonly", temperature=0.7)


__all__ = ["AskStrategy"]
