"""command：可插拔扩展三件套之「入口轴」（M14 §1.2）

斜杠命令绕过 LLM 直达功能 —— 墙上的开关 vs 语音助手。
当模型不确定时（理解偏差 / 反问 / 故障），命令是用户手里唯一的确定性把手。

宿主用法（CLI/Web 同一套）：
    registry = CommandRegistry().install_builtins()
    result = await registry.dispatch(text, CommandContext(session=..., manager=...))
    if result.kind == "direct":        print(result.text)
    elif result.kind == "prompt_inject": await loop.run(session, result.text,
                                                        mode=result.new_mode or mode)
    else:                              session.append(SystemMsg(result.text))

import 本包即触发内置命令登记（副作用）。
"""
from .parser import Command, CommandKind, CommandParser, CommandResult
from .registry import (CommandContext, CommandEntry, CommandHandler,
                       CommandRegistry, builtin_entries, register_command)
from .handlers import builtin  # noqa: F401  —— 触发 @register_command 登记

__all__ = [
    "Command", "CommandKind", "CommandParser", "CommandResult",
    "CommandContext", "CommandEntry", "CommandHandler", "CommandRegistry",
    "builtin_entries", "register_command",
]
