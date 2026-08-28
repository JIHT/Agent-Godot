"""command/registry.py —— 命令注册表 + 分发 + 近邻提示（M14 §1.2 / §4 步骤 4）

命令与工具是两套 namespace（§1.2 易错点①）：
- 命令 = **用户**入口（确定性、零 token、模型故障时仍可用——逃生舱）
- 工具 = **模型**入口（FC 声明，模型自主决策调用）
同名会疯，所以命令表里出现工具名是设计错误，反之亦然。

handler 签名：`async (cmd: Command, ctx: CommandContext) -> CommandResult`。
资源（session/manager/skills/…）走 CommandContext 而不是堆在 handler 参数上
——加一个资源不用改所有 handler 的签名。
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .parser import Command, CommandKind, CommandParser, CommandResult


@dataclass
class CommandContext:
    """命令执行时可用的资源包（由宿主 CLI/Web 组装一次，全程复用）。"""

    session: Any = None                 # 事件溯源会话（/compact /rewind 的作用对象）
    manager: Any = None                 # SessionManager（/rewind 需要事件仓库）
    skills: Any = None                  # SkillLoader（/skills）
    compressor: Any = None              # M07 Compressor（/compact）
    loop: Any = None                    # AgentLoop（需要广播事件的命令用）
    model: str = ""                     # 当前模型（/model）
    mode: str = "ask"                   # 当前模式（/plan 会改它）
    set_model: Callable[[str], Any] | None = None   # 模型切换回调（同步/异步皆可）
    project_root: Any = None            # 项目根（/checkpoint 等文件向命令用）
    registry: Any = None                # 本注册表（dispatch 时回填，供 /help 用）
    extra: dict = field(default_factory=dict)

    async def emit(self, type_: str, **payload) -> None:
        """审计广播：命令也是一次用户操作（M09 审计三问），有事件总线就发。"""
        bus = getattr(self.loop, "bus", None)
        emit_fn = getattr(bus, "emit", None)
        if emit_fn is None:
            return
        try:
            await emit_fn(type_, **payload)
        except Exception:                       # noqa: BLE001 —— 审计不许拖垮命令
            pass

    async def set_model_if_supported(self, ref: str) -> bool:
        """切换模型（回调可能是同步函数也可能是协程）。"""
        if self.set_model is None:
            return False
        out = self.set_model(ref)
        if hasattr(out, "__await__"):
            await out
        return True


CommandHandler = Callable[[Command, CommandContext],
                          Awaitable[CommandResult] | CommandResult]


@dataclass
class CommandEntry:
    name: str
    handler: CommandHandler
    help: str = ""
    usage: str = ""
    kind: CommandKind = "direct"


# 全局内置命令表：`@register_command("compact")` 写入，install_builtins 拷进实例
_BUILTINS: dict[str, CommandEntry] = {}


def register_command(name: str, *, help: str = "", usage: str = "",
                     kind: CommandKind = "direct"):
    """模块级装饰器：登记一个内置命令（import 本包即完成登记）。"""

    def deco(fn: CommandHandler) -> CommandHandler:
        _BUILTINS[name] = CommandEntry(name=name, handler=fn, help=help,
                                       usage=usage or f"/{name}", kind=kind)
        return fn

    return deco


def builtin_entries() -> list[CommandEntry]:
    return list(_BUILTINS.values())


class CommandRegistry:
    """命令表：注册 / 分发 / 近邻提示 / 帮助文本。"""

    def __init__(self, parser: CommandParser | None = None):
        self._entries: dict[str, CommandEntry] = {}
        self.parser = parser or CommandParser()

    # ---------- 注册 ----------

    def register(self, name_or_entry, handler: CommandHandler | None = None,
                 *, help: str = "", usage: str = "",
                 kind: CommandKind = "direct") -> None:
        """两种形态：`register(entry)` 或 `register("name", fn, help=...)`。"""
        if isinstance(name_or_entry, CommandEntry):
            self._entries[name_or_entry.name] = name_or_entry
            return
        if handler is None:
            raise TypeError("register(name, handler) 需要 handler")
        self._entries[name_or_entry] = CommandEntry(
            name=name_or_entry, handler=handler, help=help,
            usage=usage or f"/{name_or_entry}", kind=kind)

    def install_builtins(self) -> "CommandRegistry":
        """把 @register_command 登记的内置命令装进本注册表。"""
        for entry in builtin_entries():
            self.register(entry)
        return self

    def names(self) -> list[str]:
        return list(self._entries)

    def has(self, name: str) -> bool:
        return name in self._entries

    def entry(self, name: str) -> CommandEntry | None:
        return self._entries.get(name)

    # ---------- 分发 ----------

    async def dispatch(self, cmd: Command | str,
                       ctx: CommandContext | None = None) -> CommandResult:
        """解析（如需）→ 查表 → 执行 handler；未知名给近邻提示而非报错。"""
        if isinstance(cmd, str):
            parsed = self.parser.parse(cmd)
            if parsed is None:
                return CommandResult.direct(f"不是合法命令: {cmd.strip()}")
            cmd = parsed
        entry = self._entries.get(cmd.name)
        if entry is None:
            return CommandResult.direct(self.unknown_text(cmd.name))
        context = ctx or CommandContext()
        context.registry = self
        try:
            out = entry.handler(cmd, context)
            if hasattr(out, "__await__"):
                out = await out
        except Exception as e:                      # noqa: BLE001
            # 命令表面向人：异常翻译成一句话，不该把栈抛到终端上
            return CommandResult.direct(
                f"命令 /{cmd.name} 执行失败: {type(e).__name__}: {e}")
        return out if isinstance(out, CommandResult) else CommandResult.direct(
            str(out))

    # ---------- 近邻提示与帮助 ----------

    def suggestions(self, name: str, n: int = 3) -> list[str]:
        """/rewindd → ["rewind"]（difflib 编辑距离，§5 验收）。"""
        return difflib.get_close_matches(name, self._entries.keys(), n=n,
                                         cutoff=0.6)

    def unknown_text(self, name: str) -> str:
        near = self.suggestions(name, n=1)
        hint = f"；你是否想输入 /{near[0]}？" if near else ""
        return (f"未知命令: /{name}{hint}\n"
                f"输入 /help 查看全部命令（共 {len(self._entries)} 个）")

    def help_text(self, name: str = "") -> str:
        if name:
            e = self._entries.get(name)
            if e is None:
                return f"没有命令 /{name}"
            return f"/{e.name} — {e.help}\n用法: {e.usage}"
        lines = [f"共 {len(self._entries)} 个命令："]
        for nm in sorted(self._entries):
            e = self._entries[nm]
            lines.append(f"  /{nm:<12} {e.help}")
        lines.append("提示: 输入 /help <命令名> 看用法")
        return "\n".join(lines)


__all__ = ["CommandContext", "CommandEntry", "CommandHandler", "CommandRegistry",
           "builtin_entries", "register_command"]
