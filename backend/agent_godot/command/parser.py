"""command/parser.py —— 斜杠命令的解析与三种产出（M14 §1.2 / §2）

解析只用正则 + 空格分词（§1.2 ①）：命令保持"人类可打"的简单性，复杂参数
交给自然语言——`/plan 给 player.gd 加二段跳` 的参数就是一整句话，不用引号。

三种产出（决定"接下来谁干活"，§7 问答 5）：
- direct       ：直接渲染给用户（清单类：/skills list），不进模型，省一轮
- prompt_inject：转模型输入（路由类：/plan 打包），命令只切模式 + 注入任务
- state_change ：直接改会话状态（控制类：/compact /rewind），把"世界变了"
                 通知模型，防止它基于被截断的历史继续困惑
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CommandKind = Literal["direct", "prompt_inject", "state_change"]

# 命令名允许字母/数字/下划线/连字符；其余全是 args（不分词，保持"人类可打"）
_COMMAND_RE = re.compile(r"^/(?P<name>[\w\-]+)\s*(?P<args>.*)$", re.S)


@dataclass
class Command:
    """一条解析后的斜杠命令。"""

    name: str
    args: str = ""
    raw: str = ""

    @property
    def argv(self) -> list[str]:
        """args 按空白分词（`/skills use 打包发布` → ["use", "打包发布"]）。"""
        return self.args.split()

    @property
    def sub(self) -> str:
        """第一个词（子命令）；无参数时为空串。"""
        parts = self.argv
        return parts[0] if parts else ""

    @property
    def rest(self) -> str:
        """去掉子命令后的剩余文本。"""
        parts = self.argv
        return self.args[len(parts[0]):].strip() if parts else ""


@dataclass
class CommandResult:
    """命令的产出：kind 决定调用方怎么处理它。"""

    kind: CommandKind = "direct"
    text: str | None = None
    new_mode: str | None = None          # 模式切换（/plan → plan）
    data: dict | None = None             # 结构化回执（/skills list 的目录等）

    @classmethod
    def direct(cls, text: str, *, data: dict | None = None) -> "CommandResult":
        return cls(kind="direct", text=text, data=data)

    @classmethod
    def prompt_inject(cls, text: str, *, new_mode: str | None = None,
                      data: dict | None = None) -> "CommandResult":
        return cls(kind="prompt_inject", text=text, new_mode=new_mode, data=data)

    @classmethod
    def state_change(cls, text: str, *, new_mode: str | None = None,
                     data: dict | None = None) -> "CommandResult":
        return cls(kind="state_change", text=text, new_mode=new_mode, data=data)


class CommandParser:
    """斜杠命令解析器（无状态）。"""

    def is_command(self, text: str) -> bool:
        return bool(text) and text.lstrip().startswith("/")

    def parse(self, text: str) -> Command | None:
        """解析输入；不是命令（或不是合法命令名）返回 None。"""
        if not text:
            return None
        m = _COMMAND_RE.match(text.strip())
        if m is None:
            return None
        return Command(name=m.group("name"), args=m.group("args").strip(),
                       raw=text.strip())


__all__ = ["Command", "CommandKind", "CommandParser", "CommandResult"]
