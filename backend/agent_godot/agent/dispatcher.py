"""agent/dispatcher.py —— 工具并发调度（M03 §1.3 / M04 适配版）

工地调度员：模型一轮返回多个 tool_calls，按 readonly 元数据分流——
- 读类（无副作用）asyncio.gather 并发
- 写类（副作用）按序执行（防两个写同文件竞态覆盖）
每工具独立超时；失败不炸循环——异常翻译成 ToolResponse(ok=False) 回填。

M04 适配：工具是 BaseTool 实例，入口统一走 execute()（内部做 pydantic
参数校验），返回值就是 ToolResponse——本模块只补 call_id、超时与保序。

关键正确性：并发/串行执行完毕后，结果**按原始调用顺序**重组——
否则 Observation 与 tool_call_id 配对错乱（§1.3 易错点②）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent_godot.core import ToolCall
from agent_godot.tools import ErrorKind, ToolError, ToolRegistry, ToolResponse


@dataclass
class DispatchConfig:
    """工具超时分级（读快写慢、headless 更慢——M06 的 Godot headless 会用到）。"""
    read_timeout: float = 10.0
    write_timeout: float = 30.0
    headless_timeout: float = 120.0


class Dispatcher:
    def __init__(self, registry: ToolRegistry, config: DispatchConfig | None = None,
                 gate=None, on_result=None):
        """gate：M09 权限门（ConfirmGate）。None = 不设防（单测/沙箱）。

        on_result：M09 单调用完成回调（call, resp）→ ToolDone 事件即时落盘。
        挂起时同批已执行调用的响应必须已在事件流里——恢复点重建（§3）
        靠它，不能等整批结束再补记。
        """
        self.registry = registry
        self.config = config or DispatchConfig()
        self.gate = gate
        self.on_result = on_result

    def report_result(self, call: ToolCall, resp: ToolResponse) -> None:
        """单调用完成的即时记账（有钩子才记）。"""
        if self.on_result is not None:
            self.on_result(call, resp)

    async def execute(self, calls: list[ToolCall]) -> list[ToolResponse]:
        """调度一批工具调用，返回与 calls 顺序一致的结果列表。

        M09 接线：每个 call 前先过 gate.check()——allow 直跑、deny 短路回填
        DENIED、need_confirm 走确认门（同批并行调用"部分待确认"在此逐个解决）。
        """
        if not calls:
            return []

        results: dict[str, ToolResponse] = {}
        allowed: list[ToolCall] = []
        for c in calls:
            if self.gate is None:
                allowed.append(c)
                continue
            decision = await self.gate.check(c)
            if decision.action == "allow":
                allowed.append(c)
            elif decision.action == "deny":
                results[c.id] = _denied(c, decision.reason)
                self.report_result(c, results[c.id])
            else:                                   # need_confirm → 确认门
                # request() 内部（execute_now/拒绝）负责即时记账
                results[c.id] = await self.gate.request(c)

        read_ops = [c for c in allowed if self._is_readonly(c.name)]
        write_ops = [c for c in allowed if not self._is_readonly(c.name)]

        # 读类并发（_safe_run 已吞异常，单个失败不取消全体）
        if read_ops:
            gathered = await asyncio.gather(*(self._safe_run(c) for c in read_ops))
            for c, r in zip(read_ops, gathered):
                results[c.id] = self._finalize(c, r)
                self.report_result(c, results[c.id])

        # 写类按序（副作用工具排队，防竞态）
        for c in write_ops:
            results[c.id] = self._finalize(c, await self._safe_run(c))
            self.report_result(c, results[c.id])

        # ★ 按原始调用顺序重组（而非"读堆+写堆"的拼接序）
        return [results[c.id] for c in calls]

    async def execute_now(self, call: ToolCall) -> ToolResponse:
        """单调用直执行（M09：确认批准后的现场执行入口）。

        不过 gate——批准本身就是放行；拒绝的调用不会走到这里。
        """
        resp = self._finalize(call, await self._safe_run(call))
        self.report_result(call, resp)
        return resp

    def _is_readonly(self, name: str) -> bool:
        try:
            return self.registry.spec(name).readonly
        except KeyError:
            return True   # 未知工具按只读处理（走读超时），错误在 _safe_run 统一翻译

    async def _safe_run(self, call: ToolCall) -> ToolResponse | Exception:
        """执行单个工具：超时分类翻译，其余异常吞掉返回（不吞 CancelledError）。"""
        try:
            timeout = self._timeout_for(call.name)
            tool = self.registry.get(call.name)      # 幻觉工具名 → KeyError
            return await asyncio.wait_for(tool.execute(call.arguments),
                                          timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.TIMEOUT, tool=call.name,
                message=f"执行超过 {self._timeout_for(call.name)}s 被强制终止",
                hint="工具可能挂起，请换思路或跳过此步"))
        except KeyError as e:
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.VALIDATION, tool=call.name, message=str(e),
                hint="从可用工具列表中选择，不要编造工具名"))
        except Exception as e:      # noqa: BLE001 —— 工具错误=Observation，不是事故
            return e

    def _timeout_for(self, name: str) -> float:
        # M06：带 headless tag 的工具（godot_check/run_tests/run_scene）给 120s 档
        try:
            if "headless" in self.registry.spec(name).meta.tags:
                return self.config.headless_timeout
        except (KeyError, AttributeError):
            pass
        if "headless" in name:
            return self.config.headless_timeout
        try:
            return (self.config.read_timeout
                    if self.registry.spec(name).readonly
                    else self.config.write_timeout)
        except KeyError:
            return self.config.read_timeout

    def _finalize(self, call: ToolCall, result) -> ToolResponse:
        """统一收口：填 call_id；异常包装成 INTERNAL；非 ToolResponse 兜底。"""
        if isinstance(result, ToolResponse):
            resp = result
        elif isinstance(result, Exception):
            resp = ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.INTERNAL, tool=call.name,
                message=f"{type(result).__name__}: {result}",
                hint="检查参数后重试，或换一种方式完成"))
        else:                        # 理论不达（BaseTool.execute 返回 ToolResponse）
            resp = ToolResponse(ok=True, summary=str(result))
        resp.call_id = call.id
        return resp


def _denied(call: ToolCall, reason: str) -> ToolResponse:
    """规则直接拒（deny 短路）：拒绝也是数据，不是异常（§1.2）。"""
    from agent_godot.permission.confirm import denied_response
    return denied_response(call.id, call.name, reason)
