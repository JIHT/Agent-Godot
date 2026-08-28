"""tests/test_hooks/conftest.py —— Hook 管线测试夹具。

counting_hook：把"我被执行了"和"我看到了什么"记进共享 log——
veto 短路 / modify 次序全靠这个 log 断言（§5 的计数器断言）。
"""
from __future__ import annotations

from pydantic import BaseModel

from agent_godot.agent import Dispatcher
from agent_godot.hooks import HookContext, HookSpec
from agent_godot.tools import BaseTool, ToolMeta, ToolRegistry, ToolResponse
from agent_godot.tools.file_lock import sha16


def counting_hook(name: str, point: str = "pre_tool", priority: int = 100,
                  result=None, log: list | None = None,
                  async_: bool = False) -> HookSpec:
    """生成一个会记账的 hook spec（result 为 None 时等价于 pass）。"""
    log = log if log is not None else []

    async def handler(ctx: HookContext):
        log.append({"name": name, "args": dict(ctx.args),
                    "response": ctx.response.summary if ctx.response else None,
                    "modified_by": list(ctx.modified_by)})
        return result

    return HookSpec(name=name, point=point, priority=priority,
                    handler=handler, async_=async_)


class WriteGdTool(BaseTool):
    """写 .gd 的测试工具（模拟 godot_write_script 的返回形态：data 带 hash）。"""
    meta = ToolMeta(name="write_script", description="写入脚本（测试用）",
                    readonly=False, risk="medium")

    class Params(BaseModel):
        path: str
        content: str

    def __init__(self, root):
        self.root = root

    async def run(self, path: str, content: str) -> ToolResponse:
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResponse(ok=True, summary=f"已写入 {path}",
                            data={"hash": sha16(content)})


class EchoTool(BaseTool):
    """回显工具（只读）：把 x 原样回显，post_tool 改写的观察对象。"""
    meta = ToolMeta(name="echo", description="回显（测试用）",
                    readonly=True, risk="low")

    class Params(BaseModel):
        x: str = "ok"

    async def run(self, x: str = "ok") -> ToolResponse:
        return ToolResponse(ok=True, summary=x)


def make_dispatcher(root=None) -> Dispatcher:
    reg = ToolRegistry()
    reg.register(EchoTool())
    if root is not None:
        reg.register(WriteGdTool(root))
    return Dispatcher(reg)


__all__ = ["EchoTool", "WriteGdTool", "counting_hook", "make_dispatcher"]
