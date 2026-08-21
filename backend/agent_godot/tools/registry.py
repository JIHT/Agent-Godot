"""tools/registry.py —— 工具注册表（M04 §1.1 的最小版）

装饰器注册制（M02 的 PROVIDERS 同款思想）：
    @registry.register(name="list_files", description="...",
                       parameters={...}, readonly=True)
    async def list_files(path): ...

核心职责：
- register：登记工具（名字/说明/JSON Schema/只读标记）
- spec(name)：查单个工具（dispatcher 读 readonly 元数据、校验工具名真实性）
- tool_specs()：导出全部 ToolSpec 列表（loop 喂给 LLM 的 tools 参数）
"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass

from agent_godot.core import ToolSpec


@dataclass
class Tool:
    """一个已注册工具：元数据 + 可调用函数。"""
    name: str
    description: str
    parameters: dict          # JSON Schema（properties / required）
    readonly: bool            # True=无副作用可并发；False=有副作用按序执行
    fn: Callable              # 工具实现（同步或 async 皆可）

    def to_spec(self) -> ToolSpec:
        """转成喂给 LLM 的 core.ToolSpec（FC 声明）。"""
        return ToolSpec(self.name, self.description, self.parameters)

    async def run(self, **kwargs):
        """执行工具。兼容同步/异步两种实现，统一返回字符串。"""
        result = self.fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, *, name: str, description: str, parameters: dict,
                 readonly: bool = True) -> Callable:
        """装饰器：把函数登记为工具。"""
        def deco(fn: Callable) -> Callable:
            self._tools[name] = Tool(name, description, parameters, readonly, fn)
            return fn
        return deco

    def spec(self, name: str) -> Tool:
        """查单个工具（含 readonly 元数据）。未注册抛 KeyError。"""
        return self._tools[name]

    def has(self, name: str) -> bool:
        """工具名真实性校验（M03 §1.1 易错点④：防模型幻觉工具名）。"""
        return name in self._tools

    def tool_specs(self) -> list[ToolSpec]:
        """导出全部 FC 声明，喂给 LLMRequest.tools。"""
        return [t.to_spec() for t in self._tools.values()]
