"""agent/dispatcher.py —— 工具并发调度（M03 §1.3 / §4 步骤 3）

工地调度员：模型一轮返回多个 tool_calls，按 readonly 元数据分流——
- 读类（无副作用）asyncio.gather 并发
- 写类（副作用）按序执行（防两个写同文件竞态覆盖）
每个工具独立超时；失败不炸循环——异常翻译成 ToolResponse(ok=False) 回填。

关键正确性：并发/串行执行完毕后，结果**按原始调用顺序**重组——
否则 Observation 与 tool_call_id 配对错乱（§1.3 易错点②）。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from agent_godot.core import ToolCall
from agent_godot.tools import ToolRegistry, ToolResponse


@dataclass
class DispatchConfig:
    """工具超时分级（读快写慢、headless 更慢——M06 的 Godot headless 会用到）。"""
    read_timeout: float = 10.0
    write_timeout: float = 30.0
    headless_timeout: float = 120.0


class Dispatcher:
    def __init__(self, registry: ToolRegistry, config: DispatchConfig | None = None):
        self.registry = registry
        self.config = config or DispatchConfig()

    async def execute(self, calls: list[ToolCall]) -> list[ToolResponse]:
        """调度一批工具调用，返回与 calls 顺序一致的结果列表。"""
        if not calls:
            return []

        read_ops = [c for c in calls if self._is_readonly(c.name)]
        write_ops = [c for c in calls if not self._is_readonly(c.name)]

        results: dict[str, ToolResponse] = {}

        # 读类并发（_safe_run 已吞异常，单个失败不取消全体）
        if read_ops:
            gathered = await asyncio.gather(*(self._safe_run(c) for c in read_ops))
            for c, r in zip(read_ops, gathered):
                results[c.id] = self._to_response(c, r)

        # 写类按序（副作用工具排队，防竞态）
        for c in write_ops:
            results[c.id] = self._to_response(c, await self._safe_run(c))

        # ★ 按原始调用顺序重组（而非"读堆+写堆"的拼接序）
        return [results[c.id] for c in calls]

    def _is_readonly(self, name: str) -> bool:
        try:
            return self.registry.spec(name).readonly
        except KeyError:
            return True   # 未知工具当只读处理，真实错误在 _run_one 里统一转 ok=False

    async def _safe_run(self, call: ToolCall):
        """执行单个工具，异常吞掉转成返回值（Exception，不吞 CancelledError）。"""
        try:
            return await self._run_one(call)
        except Exception as e:          # noqa: BLE001 —— 工具错误=Observation，不是事故
            return e

    async def _run_one(self, call: ToolCall):
        """执行单个工具：解析参数 → 独立超时 → 执行。抛出的异常交给 _to_response。"""
        if not self.registry.has(call.name):
            raise ValueError(f"未注册的工具: {call.name}（模型幻觉工具名）")
        tool = self.registry.spec(call.name)
        try:
            args = json.loads(call.arguments) if call.arguments else {}
        except json.JSONDecodeError:
            raise ValueError(f"工具参数不是合法 JSON: {call.arguments[:100]}") from None
        timeout = self.config.headless_timeout if "headless" in call.name \
            else (self.config.write_timeout if not tool.readonly
                  else self.config.read_timeout)
        return await asyncio.wait_for(tool.run(**args), timeout=timeout)

    def _to_response(self, call: ToolCall, result) -> ToolResponse:
        """统一包装：异常 → ok=False 带错误描述；正常 → ok=True 序列化为字符串。"""
        if isinstance(result, Exception):
            return ToolResponse(call_id=call.id, ok=False,
                                error=f"{type(result).__name__}: {result}")
        return ToolResponse(call_id=call.id, ok=True, data=str(result))
