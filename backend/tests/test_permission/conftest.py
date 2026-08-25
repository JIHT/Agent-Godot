"""tests/test_permission/conftest.py —— M09 权限测试夹具。

EchoTool（只读 low）/ MarkTool（写 medium，追加标记到文件，副作用可断言）/
DeleteTool（写 high）。剧本式 prompter 模拟家属签字（批/拒/不再问）。
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from agent_godot.core import ToolCall
from agent_godot.permission.confirm import ConfirmAnswer, ConfirmGate
from agent_godot.permission.gate import PermissionGate
from agent_godot.permission.rules import RuleEngine
from agent_godot.tools import BaseTool, ToolMeta, ToolRegistry, ToolResponse


class EchoTool(BaseTool):
    meta = None

    class Params(BaseModel):
        x: str = "ok"

    async def run(self, x: str = "ok") -> ToolResponse:
        return ToolResponse(ok=True, summary=f"echo:{x}")


class MarkTool(BaseTool):
    """写工具：向 path 追加一行标记（副作用计数器，验证"无二次副作用"）。"""
    meta = None

    class Params(BaseModel):
        path: str
        tag: str = "m"

    async def run(self, path: str, tag: str = "m") -> ToolResponse:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{tag}\n")
        return ToolResponse(ok=True, summary=f"marked {path} with {tag}")


class DeleteTool(BaseTool):
    meta = None

    class Params(BaseModel):
        path: str

    async def run(self, path: str) -> ToolResponse:
        return ToolResponse(ok=True, summary=f"deleted {path}")


def _attach(tool_cls, name: str, *, readonly: bool, risk: str):
    tool_cls.meta = ToolMeta(name=name, description=name,
                             readonly=readonly, risk=risk)
    return tool_cls


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_attach(EchoTool, "echo", readonly=True, risk="low")())
    reg.register(_attach(MarkTool, "mark", readonly=False, risk="medium")())
    reg.register(_attach(DeleteTool, "delete_file", readonly=False, risk="high")())
    return reg


def make_rules(config: dict | None = None, project_root: str | None = "/proj"):
    return RuleEngine(config=config, project_root=project_root)


def make_gate(session=None, rules=None, dispatcher=None, *,
              prompter=None, timeout: float = 86400.0):
    from agent_godot.agent.dispatcher import Dispatcher
    rules = rules or make_rules()
    dispatcher = dispatcher or Dispatcher(make_registry())
    return ConfirmGate(rules, session, dispatcher,
                       registry=dispatcher.registry,
                       prompter=prompter, timeout=timeout)


def call(cid: str, tool: str, **args) -> ToolCall:
    import json
    return ToolCall(id=cid, name=tool,
                    arguments=json.dumps(args, ensure_ascii=False))


def approve(pc) -> ConfirmAnswer:
    return ConfirmAnswer(approved=True)


def deny(pc) -> ConfirmAnswer:
    return ConfirmAnswer(approved=False, reason="不想让你改这个")


def allow_session(pc) -> ConfirmAnswer:
    return ConfirmAnswer(approved=True, remember="session")
