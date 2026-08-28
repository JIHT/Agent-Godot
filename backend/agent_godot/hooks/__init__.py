"""hooks：可插拔扩展三件套之「横切轴」（M14 §1.1）

六挂载点 × 三动作协议（pass / modify / veto）：
    pre_tool / post_tool / pre_loop / post_loop / session_start / session_end

谁在用：
- Dispatcher（M03）跑 pre_tool / post_tool，并把 HookVeto 翻译成 DENIED 响应
- AgentLoop（M03）跑 pre_loop（可注入消息）/ post_loop
- CLI 会话壳 跑 session_start / session_end，退出前 join_background()

新增能力不改核心：写一个 handler（类或函数）→ `pipeline.register(spec)`。
"""
from __future__ import annotations

from pathlib import Path

from .pipeline import (HOOK_POINTS, HookContext, HookPipeline, HookResult,
                       HookSpec, HookVeto)
from .post_tool import FormatHook, RedactHook, format_gdscript
from .pre_tool import PermissionHook

__all__ = [
    "HOOK_POINTS", "HookContext", "HookPipeline", "HookResult", "HookSpec",
    "HookVeto",
    "PermissionHook", "FormatHook", "RedactHook", "format_gdscript",
    "build_default_pipeline",
]


def build_default_pipeline(*, root: Path | None = None, gate=None,
                           dispatcher=None, bus=None,
                           format_root: Path | None = None,
                           hook_timeout: float = 0.0) -> HookPipeline:
    """一键装配默认管线（CLI / 测试共用的组装入口）。

    装配顺序即 priority 段位（§7 问答 3）：
      permission p=0  系统级（可否决）
      redact     p=90 安全类
      format     p=100 业务类（仅在给了项目根时挂——不知道根就格式化不了）
    """
    pipeline = HookPipeline(bus=bus, hook_timeout=hook_timeout)
    if gate is not None:
        pipeline.register(PermissionHook(gate, dispatcher).spec())
    pipeline.register(RedactHook().spec())
    if format_root is not None or root is not None:
        pipeline.register(FormatHook(format_root or root).spec())
    return pipeline
