"""tools/registry.py —— 洞洞板注册表（M04 §1.2 完整版）

"定义即注册即声明"：一个工具类同时产出三样东西——
① FC 声明（tool_specs() 喂模型：Schema 从 pydantic Params 自动生成）
② 执行入口（BaseTool.execute：JSON 参数 → pydantic 校验 → run()）
③ 元数据（ToolMeta：readonly 供 Dispatcher 分流、risk 供 M09 权限、
   tags 供 M13 模式过滤——ask 模式物理上不给写工具，工具集即能力边界）

与 M02 的 @register_provider 同一思想："一切皆插件"的第二次落地。
"""
from __future__ import annotations

import copy
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ValidationError

from agent_godot.core import ToolSpec
from .response import ErrorKind, ToolError, ToolResponse
from .schema import to_fc_schema


@dataclass
class ToolMeta:
    """工具身份证：Dispatcher 分流、M09 权限分级、M13/M15 过滤全靠它。"""
    name: str
    description: str
    readonly: bool = True                              # Dispatcher 读写分流依据
    risk: Literal["low", "medium", "high"] = "low"     # M09 权限分级依据
    tags: set[str] = field(default_factory=set)        # 模式/子代理过滤依据


class BaseTool(ABC):
    """工具基类：子类定义 docstring（=description）+ Params（=Schema）+ run。"""

    meta: ToolMeta
    Params: type[BaseModel]

    @property
    def readonly(self) -> bool:
        """便利属性：Dispatcher 的 `registry.spec(name).readonly` 调用形态。"""
        return self.meta.readonly

    async def execute(self, arguments: str) -> ToolResponse:
        """Dispatcher 统一入口：JSON 字符串 → pydantic 校验 → run()。

        校验失败返回 VALIDATION 错误响应（不抛——错误也是数据）。
        """
        try:
            params = self.Params(**(json.loads(arguments) if arguments else {}))
        except json.JSONDecodeError:
            return ToolResponse(ok=False, error=ToolError(
                ErrorKind.VALIDATION, self.meta.name,
                f"参数不是合法 JSON: {arguments[:100]!r}",
                hint="按工具参数 schema 修正后重试"))
        except ValidationError as e:
            return ToolResponse(ok=False, error=ToolError(
                ErrorKind.VALIDATION, self.meta.name,
                f"参数校验失败: {[err['msg'] for err in e.errors()[:3]]}",
                hint="按参数 schema（类型/必填）修正后重试"))
        return await self.run(**params.model_dump())

    @abstractmethod
    async def run(self, **params) -> ToolResponse: ...

    def to_spec(self) -> ToolSpec:
        """转成 core.ToolSpec（喂给 LLMRequest.tools 的 FC 声明）。"""
        return ToolSpec(self.meta.name, self.meta.description,
                        to_fc_schema(self.Params))


# 全局注册表：@register_tool 装饰器写入（build 时由 ToolRegistry.from_global 收集）
_GLOBAL_TOOLS: dict[str, type[BaseTool]] = {}


def register_tool(*, name: str, readonly: bool = True,
                  risk: Literal["low", "medium", "high"] = "low",
                  tags: set[str] | None = None):
    """类装饰器：从子类提取 docstring 当 description，组装 ToolMeta 挂上。"""
    def deco(cls: type[BaseTool]) -> type[BaseTool]:
        cls.meta = ToolMeta(name=name, description=(cls.__doc__ or "").strip(),
                            readonly=readonly, risk=risk, tags=tags or set())
        _GLOBAL_TOOLS[name] = cls
        return cls
    return deco


class _NamespacedProxy(BaseTool):
    """命名空间代理：M05 的 MCP 桥接用（mcp__godot__read_scene 前缀防重名）。"""

    def __init__(self, inner: BaseTool, new_name: str):
        self._inner = inner
        self.meta = copy.replace(inner.meta, name=new_name)  # type: ignore[arg-type]
        self.Params = inner.Params

    async def run(self, **params) -> ToolResponse:
        return await self._inner.run(**params)


class ToolRegistry:
    """洞洞板：注册/查询/过滤/命名空间视图。"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.meta.name] = tool

    @classmethod
    def from_global(cls) -> "ToolRegistry":
        """从全局装饰器注册表构建（实例化所有已 @register_tool 的类）。"""
        reg = cls()
        for tool_cls in _GLOBAL_TOOLS.values():
            reg.register(tool_cls())
        return reg

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"未注册的工具: {name}（可用: {sorted(self._tools)}）")
        return self._tools[name]

    def has(self, name: str) -> bool:
        """工具名真实性校验（防模型幻觉工具名）。"""
        return name in self._tools

    def spec(self, name: str) -> BaseTool:
        """查单个工具（M03 Dispatcher 的调用形态，等价 get）。"""
        return self.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def tool_specs(self) -> list[ToolSpec]:
        """全部工具的 FC 声明（喂给 LLMRequest.tools）。"""
        return [t.to_spec() for t in self._tools.values()]

    def filter(self, *, tags: set[str] | None = None,
               readonly: bool | None = None) -> "ToolRegistry":
        """视图裁剪：ask 模式只给只读工具 / 子代理白名单——物理能力边界。"""
        view = ToolRegistry()
        for t in self._tools.values():
            if readonly is not None and t.meta.readonly != readonly:
                continue
            if tags is not None and not (t.meta.tags & tags):
                continue
            view.register(t)
        return view

    def namespaced(self, ns: str) -> "ToolRegistry":
        """加前缀的视图副本（M05 MCP 桥接：mcp__godot__ 前缀防本地工具重名）。"""
        view = ToolRegistry()
        for t in self._tools.values():
            view.register(_NamespacedProxy(t, f"{ns}__{t.meta.name}"))
        return view
