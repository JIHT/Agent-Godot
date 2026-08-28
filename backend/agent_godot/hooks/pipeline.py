"""hooks/pipeline.py —— Hook 管线：挂载点 + 优先级 + pass/modify/veto（M14 §1.1）

把 Web 框架的 middleware 搬进 Agent：
- pre_tool 链  = request middleware（进来挨个过：鉴权 → 审计 → 参数改写）
- post_tool 链 = response middleware（出去挨个过：脱敏 → 格式化）
- veto         = 中间件直接短路返回 403（本模块抛 HookVeto，由 Dispatcher 翻译）
- modify       = 中间件改写 request.body 后放行（后续 hook 看到改后的 ctx）
- async_ hook  = 中间件里的后台任务（fire-and-forget，session_end 时 join 兜底）

六挂载点：pre_tool / post_tool / pre_loop / post_loop / session_start / session_end。

三动作协议（§1.1 ①）：
- pass   → handler 返回 None（或 HookResult(action="pass")）
- modify → 改写 args / response / 注入 messages，链式传给下一个 hook
- veto   → 抛 HookVeto 短路，后续 hook 全跳过（若携带 response 则以它为结果，
           这是 M09 确认门"批准后在 hook 内执行"的落地形态）

★ 优先级段位约定（§7 问答 3）：
  0-49   系统级（权限门禁）——永远最先跑
  50-99  安全类（脱敏）——最终裁决权归安全，所以排在业务之后写入的值才生效
  100+   业务类（格式化、统计上报）
同段位内按 (priority, name) 稳定排序（注册顺序不影响结果，测试可钉死）。
"""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Literal

from agent_godot.core import Message
from agent_godot.tools import ToolResponse

HookPoint = Literal["pre_tool", "post_tool", "pre_loop", "post_loop",
                    "session_start", "session_end"]

HOOK_POINTS: tuple[str, ...] = ("pre_tool", "post_tool", "pre_loop",
                                "post_loop", "session_start", "session_end")

HookAction = Literal["pass", "modify", "veto"]


@dataclass
class HookContext:
    """一次 hook 调用的现场（在链上流动、可被 modify 改写）。

    字段分组：
    - 工具现场：tool / call_id / args / response（pre_tool 只有前三个，
      post_tool 才有 response）
    - 循环现场：session / messages（pre_loop、session_start 的注入位）
    - 审计：modified_by（谁改过这个 ctx，进 trace）
    """

    point: str = ""
    tool: str = ""
    call_id: str = ""
    args: dict = field(default_factory=dict)
    response: ToolResponse | None = None
    session: Any = None
    messages: list[Message] = field(default_factory=list)
    modified_by: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def evolve(self, **changes) -> "HookContext":
        """返回改写后的新 ctx（链式传递的载体——不原地改，方便审计与回滚）。"""
        return replace(self, **changes)


@dataclass
class HookSpec:
    """一个 hook 的注册声明（名字 + 挂载点 + 优先级 + 是否后台）。"""

    name: str
    point: HookPoint
    priority: int = 100                 # 小者先执行
    async_: bool = False                # True = 后台跑，不阻塞管线
    handler: Callable[[HookContext], Awaitable["HookResult | None"] | "HookResult | None"] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.point not in HOOK_POINTS:
            raise ValueError(
                f"未知挂载点: {self.point!r}（可用: {list(HOOK_POINTS)}）")
        if not callable(self.handler):
            raise TypeError(f"hook {self.name} 的 handler 必须是可调用对象")


@dataclass
class HookResult:
    """handler 的返回值（None 等价于 pass）。"""

    action: HookAction = "pass"
    modified_args: dict | None = None          # pre_tool：改写工具入参
    modified_response: ToolResponse | None = None   # post_tool：改写 Observation
    messages: list[Message] | None = None      # pre_loop/session_start：注入消息
    reason: str = ""                           # modify/veto 理由 → 审计日志
    reported: bool = False                     # veto 携带的响应是否已入账（防重复落盘）

    @classmethod
    def modify(cls, *, args: dict | None = None,
               response: ToolResponse | None = None,
               messages: list[Message] | None = None,
               reason: str = "") -> "HookResult":
        return cls(action="modify", modified_args=args,
                   modified_response=response, messages=messages, reason=reason)

    @classmethod
    def veto(cls, reason: str = "", *, response: ToolResponse | None = None,
             reported: bool = False) -> "HookResult":
        """短路：后续 hook 与工具执行全部跳过。

        response：M09 确认门专用——批准后的现场执行结果（或拒绝响应）由
        hook 自己产出并带回，Dispatcher 直接把它当 Observation（否则执行两次）。
        reported：该响应是否已被 hook 侧记账（confirm 内部已 report_result）。
        """
        return cls(action="veto", reason=reason, modified_response=response,
                   reported=reported)


class HookVeto(Exception):
    """veto 的异常形态：管线内短路，Dispatcher 捕获后翻译成 ToolResponse。"""

    def __init__(self, hook_name: str, reason: str = "", *,
                 response: ToolResponse | None = None, reported: bool = False):
        self.hook_name = hook_name
        self.reason = reason
        self.response = response
        self.reported = reported
        super().__init__(f"hook {hook_name} 否决: {reason}")


class HookPipeline:
    """优先级排序的同步/异步执行管线（§1.1 ③）。"""

    def __init__(self, bus=None, hook_timeout: float = 0.0):
        self._hooks: dict[str, list[HookSpec]] = {p: [] for p in HOOK_POINTS}
        self._bg: set[asyncio.Task] = set()
        self.bus = bus                      # 可选：hook 事件广播（审计/trace）
        self.hook_timeout = hook_timeout    # >0 时单 hook 超时即跳过（防恶意阻塞）
        self.trace: list[dict] = []         # 审计：veto/modify/异常全记录

    # ---------- 注册 ----------

    def register(self, spec: HookSpec) -> None:
        """追加并按 (priority, name) 稳定排序——注册顺序不影响执行顺序。"""
        self._hooks[spec.point].append(spec)
        self._hooks[spec.point].sort(key=lambda s: (s.priority, s.name))

    def unregister(self, name: str) -> bool:
        """按名字摘掉 hook（测试环境禁用权限 hook 一行配置，§7 问答 9）。"""
        for specs in self._hooks.values():
            for i, s in enumerate(specs):
                if s.name == name:
                    specs.pop(i)
                    return True
        return False

    def has(self, point: str) -> bool:
        return bool(self._hooks.get(point))

    def specs(self, point: str | None = None) -> list[HookSpec]:
        if point is not None:
            return list(self._hooks[point])
        return [s for p in HOOK_POINTS for s in self._hooks[p]]

    def names(self, point: str | None = None) -> list[str]:
        return [s.name for s in self.specs(point)]

    # ---------- 执行 ----------

    async def run(self, point: str, ctx: HookContext) -> HookContext:
        """跑一个挂载点的全部 hook，返回（可能被 modify 过的）ctx。

        - async_ hook：建后台任务就走，不等（不拖慢每个工具调用）
        - 同步 hook 返回 None → pass；modify → 链式传递；veto → 抛 HookVeto
        - 同步 hook 抛异常 → 吞掉并记录（横切逻辑不能拖死主流程）
        """
        if point not in self._hooks:
            raise ValueError(f"未知挂载点: {point!r}（可用: {list(HOOK_POINTS)}）")
        ctx = ctx.evolve(point=point)
        for spec in list(self._hooks[point]):     # 快照：防止 handler 里再注册
            if spec.async_:
                self._spawn(spec, ctx)
                continue
            result = await self._call(spec, ctx)
            ctx = self._apply(spec, result, ctx)
        return ctx

    async def join_background(self, timeout: float | None = None) -> int:
        """session_end 兜底：等全部后台 hook 落地（优雅关闭的标准组成）。

        返回等待的任务数；超时未完成的留给调用方决定（不强制取消——
        正在写的审计记录取消掉等于数据丢失）。
        """
        tasks = [t for t in self._bg if not t.done()]
        self._bg.clear()
        if not tasks:
            return 0
        if timeout is None:
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            await asyncio.wait(tasks, timeout=timeout)
        return len(tasks)

    def pending_background(self) -> int:
        return sum(1 for t in self._bg if not t.done())

    def veto_events(self) -> list[dict]:
        return [e for e in self.trace if e["action"] == "veto"]

    # ---------- 内部 ----------

    async def _call(self, spec: HookSpec, ctx: HookContext):
        try:
            if self.hook_timeout and self.hook_timeout > 0:
                return await asyncio.wait_for(_invoke(spec, ctx),
                                              timeout=self.hook_timeout)
            return await _invoke(spec, ctx)
        except (asyncio.TimeoutError, TimeoutError):
            self._record(spec.name, "timeout",
                         f"超过 {self.hook_timeout}s 被跳过")
            return None
        except HookVeto:
            raise                                   # veto 是协议不是事故
        except Exception as e:                      # noqa: BLE001 —— 横切逻辑不许炸主流程
            self._record(spec.name, "error", f"{type(e).__name__}: {e}")
            return None

    def _apply(self, spec: HookSpec, result: HookResult | None,
               ctx: HookContext) -> HookContext:
        if result is None or result.action == "pass":
            return ctx
        if result.action == "veto":
            self._record(spec.name, "veto", result.reason)
            raise HookVeto(spec.name, result.reason,
                           response=result.modified_response,
                           reported=result.reported)
        if result.action != "modify":
            return ctx
        self._record(spec.name, "modify", result.reason)
        if result.modified_args is not None:
            ctx = ctx.evolve(args=dict(result.modified_args),
                             modified_by=[*ctx.modified_by, spec.name])
        if result.modified_response is not None:
            ctx = ctx.evolve(response=result.modified_response,
                             modified_by=[*ctx.modified_by, spec.name])
        if result.messages:
            ctx = ctx.evolve(messages=[*ctx.messages, *result.messages])
        return ctx

    def _spawn(self, spec: HookSpec, ctx: HookContext) -> None:
        task = asyncio.ensure_future(self._bg_run(spec, ctx))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _bg_run(self, spec: HookSpec, ctx: HookContext) -> None:
        try:
            result = await _invoke(spec, ctx)
        except Exception as e:                      # noqa: BLE001
            self._record(spec.name, "error", f"{type(e).__name__}: {e}")
            return
        if result is not None and result.action != "pass":
            self._record(spec.name, result.action, result.reason)

    def _record(self, hook: str, action: str, reason: str = "") -> None:
        self.trace.append({"ts": time.time(), "hook": hook,
                           "action": action, "reason": reason})


async def _invoke(spec: HookSpec, ctx: HookContext):
    """调 handler：兼容同步返回与协程返回两种写法。"""
    res = spec.handler(ctx)
    if inspect.isawaitable(res):
        res = await res
    return res


__all__ = ["HOOK_POINTS", "HookAction", "HookContext", "HookPipeline",
           "HookResult", "HookSpec", "HookVeto"]
