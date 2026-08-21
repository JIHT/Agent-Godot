"""tools/builtin/file_tools.py —— 文件读写内置工具（M03 验收 demo 用）

两个只读工具：list_files（列目录）+ read_file（读文件）。
只读 → 可被 dispatcher 并发调度（无副作用，互不干扰）。

安全边界（M04 正式版会强化为 sandbox.py）：
- read_file 限制大小（默认 10 万字节），防一次性读入巨型文件撑爆上下文
- 异常不抛出，转成错误字符串返回（工具级失败 = Observation，不是事故）
"""
from __future__ import annotations

import os


async def list_files(path: str = ".") -> str:
    """列目录内容。path 不存在返回错误说明。"""
    try:
        entries = os.listdir(path)
    except FileNotFoundError:
        return f"路径不存在: {path}"
    except NotADirectoryError:
        return f"不是目录: {path}"
    if not entries:
        return f"{path} 是空目录"
    return "\n".join(f"- {e}" for e in sorted(entries))


async def read_file(path: str, max_bytes: int = 100_000) -> str:
    """读文本文件（限制大小）。返回文件内容或错误说明。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read(max_bytes + 1)
    except FileNotFoundError:
        return f"文件不存在: {path}"
    except IsADirectoryError:
        return f"是目录而非文件: {path}"
    except PermissionError:
        return f"无读取权限: {path}"
    truncated = len(content) > max_bytes
    if truncated:
        content = content[:max_bytes]
    return content + ("\n...[截断]" if truncated else "")
