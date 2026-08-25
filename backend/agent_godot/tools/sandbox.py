"""tools/sandbox.py —— 车间安全规程（M04 §1.4）

三道闸：
① 执行前：路径规范化 + 项目根白名单 + 敏感目录拒绝（防 path traversal——
   提示注入可诱导模型读 .git/config 或 ../../.ssh/id_rsa，都是攻击面）
② 执行中：分级超时（dispatcher 的 wait_for；本模块提供清理钩子）
③ 执行后：输出截断保头保尾（错误信息常在尾部 traceback，只保头会丢关键）
"""
from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath

# 敏感目录黑名单：仓库内部/环境/缓存，模型无正当理由访问
# .godot = Godot 导入缓存（上万文件）；.agent_godot = M06 检查点仓库（Agent 自管）
DENY_PARTS = {".git", ".env", "__pycache__", ".venv", "node_modules", ".idea",
              ".godot", ".agent_godot"}


class PathEscapeError(Exception):
    """路径越出项目根（path traversal 被拦截）。"""


class DeniedPathError(Exception):
    """路径命中敏感目录黑名单。"""


def resolve_in_root(root: Path, rel: str) -> Path:
    """路径解析三连：posix 化 → 根白名单校验 → 敏感目录拒绝。

    Windows 坑（§1.4 易错点①）：反斜杠与大小写差异会让 is_relative_to
    判定漂移——先统一 posix 化再 resolve。
    """
    root = root.resolve()
    normalized = str(PurePosixPath(rel.replace("\\", "/")))
    p = (root / normalized).resolve()
    if not p.is_relative_to(root):
        raise PathEscapeError(f"{rel!r} 越出项目根目录")
    if any(part in DENY_PARTS for part in p.parts):
        raise DeniedPathError(str(p))
    return p


def truncate(text: str, head: int = 1500, tail: int = 300) -> str:
    """保头保尾截断：头 1500 + 尾 300 + 中间省略标记（信息量/token 的最优折中）。"""
    if len(text) <= head + tail + 50:
        return text
    return f"{text[:head]}\n...[中间省略 {len(text) - head - tail} 字符]...\n{text[-tail:]}"


async def run_with_timeout(coro, seconds: float, tool_name: str = "?",
                           on_cancel=None) -> object:
    """超时包裹：超时 → 调 on_cancel 清理（杀子进程等）→ 返回 TIMEOUT 响应（不抛）。

    与 Dispatcher 内置超时的分工：Dispatcher 用 wait_for 做总闸，
    本函数供需要精细清理的工具内部使用（M06 headless 的 Process.kill 前置）。
    """
    from .response import ErrorKind, ToolError, ToolResponse
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except (asyncio.TimeoutError, TimeoutError):
        if on_cancel:
            try:
                on_cancel()
            except Exception:  # noqa: BLE001 —— 清理失败不掩盖超时事实
                pass
        return ToolResponse(ok=False, error=ToolError(
            kind=ErrorKind.TIMEOUT, tool=tool_name,
            message=f"执行超过 {seconds}s 被强制终止",
            hint="工具可能挂起，请换思路或跳过此步"))
