"""context/builder.py —— 分区预算 + 贪心降级总装（M07 §1.2 / §4 步骤 4）

每轮开始前把书桌整理成"此刻最该看的东西"：
  算预算（窗口 - 输出保留 - tools 实测）→ 组装四分区（带优先级）
  → while 超预算：取最高可压优先级分区 downgrade → assemble 按序拼装。

降级链（先压最不痛的）：history A档占位 → B档模板摘要 → C档LLM摘要
→ memory 丢弃 → latest L1 截断（最后兜底）→ 仍超则硬失败（绝不静默丢 system）。

滞后带：超 85% 触发继续压缩、压到 70% 停——压缩本身花钱，阈值不分离会抖动。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from ..core import Message, ToolSpec
from .compressor import Compressor
from .history import HistoryConfig, HistoryManager
from .token_counter import TokenCounter
from .truncator import ObservationTruncator

# M08：记忆注入回调——async (session) -> list[Message]
# 由 memory.make_memory_provider(retriever, project_id) 构造，None=不注入
MemoryProvider = Callable[[Any], Awaitable["list[Message] | None"]]


class ContextOverflowError(Exception):
    """降级链用尽仍超预算——硬失败提示开新会话（不能静默丢 system）。"""


@dataclass
class BudgetConfig:
    """128k 窗口配额（各分区带优先级，0=不可压）。"""
    window: int = 128_000
    reserved_output: int = 8_000           # 输出保留（模型要留地方说话）
    tools_budget: int = 5_000              # tools 实测低于此值按实测扣
    latest_obs_floor: int = 16_000         # 最新观察保底（工作记忆）
    history_share: float = 0.65            # 历史分区软上限（占可用预算）
    compress_trigger: float = 0.85         # 滞后带：超 85% 触发
    compress_target: float = 0.70          # 压到 70% 停


@dataclass
class Partition:
    """一个上下文分区：名字 / 内容（list[Message] 或空）/ 压缩优先级。"""
    name: str
    content: Any = field(default_factory=list)
    priority: int = 0                      # 0=不可压，越大越先压
    tokens: int = 0
    level: int = 0                         # history 的降级档位 0→3

    @property
    def compressible(self) -> bool:
        return self.priority > 0


class ContextBuilder:
    """M03 Loop 的每轮上下文总装（替代"简单拼接"版）。"""

    def __init__(self, counter: TokenCounter | None = None,
                 compressor: Compressor | None = None,
                 config: BudgetConfig | None = None,
                 history: HistoryManager | None = None,
                 truncator: ObservationTruncator | None = None,
                 model: str = "",
                 memory_provider: MemoryProvider | None = None):
        self.counter = counter or TokenCounter()
        self.compressor = compressor or Compressor()
        self.config = config or BudgetConfig()
        self.history = history or HistoryManager(self.counter)
        self.truncator = truncator or ObservationTruncator()
        self.model = model                 # 临界精算用
        self.memory_provider = memory_provider   # M08 记忆注入回调（None=不注入）
        self._layout: dict[str, int] = {}
        self._last_estimate = 0            # 上轮请求的估算值（calibrate 对账用）

    # ---------- 主入口 ----------

    async def build(self, session, tools: list[ToolSpec] | None = None
                    ) -> list[Message]:
        cfg = self.config
        tools_cost = self.counter.estimate_tools(tools)
        budget = cfg.window - cfg.reserved_output - max(tools_cost, 0)

        msgs = list(session.messages)
        # 分区切分：system 独立（首部高召回区）；其余按轮界切 latest/history
        system_msgs = [m for m in msgs if m.role == "system"]
        rest = [m for m in msgs if m.role != "system"]
        latest, history = self._split_recent(rest)

        parts = [
            Partition("system", system_msgs, priority=0),      # 不可压
            Partition("memory", [], priority=1),               # M08 注入位（可丢）
            Partition("history", history, priority=2),         # 可压可丢
            Partition("latest", latest, priority=0),           # 不可压（工作记忆）
        ]
        # 历史先过保留配对滑窗（软上限 = 可用预算 × history_share）
        hist_cap = int(budget * cfg.history_share)
        parts[2].content = self.history.rolling(history, max_tokens=hist_cap)

        # M08：记忆分区注入（可丢——降级链优先级 1，预算紧时第一个被丢弃）
        if self.memory_provider is not None and not parts[1].content:
            try:
                memory_msgs = await self.memory_provider(session)
                parts[1].content = memory_msgs or []
            except Exception:                       # noqa: BLE001 —— 记忆不可拖垮主流程
                parts[1].content = []

        used = self._refresh(parts)

        # ---- 阶段 1：硬预算（不超窗）——贪心降级链 ----
        guard = 0
        while used > budget and guard < 8:
            guard += 1
            target = self._pick_downgrade(parts)
            if target is None:
                break
            await self._downgrade(target)
            used = self._refresh(parts)

        # ---- 最后兜底：latest 的肥 Observation 做 L1 截断 ----
        if used > budget:
            self._truncate_latest(parts[3], budget - (used - parts[3].tokens))
            used = self._refresh(parts)

        # ---- 临界精算：估算贴近预算线 ±500 时 tiktoken 复核 ----
        if self.model and abs(used - budget) <= 500:
            exact_used = self.counter.exact(self._assemble(parts), self.model)
            if exact_used > budget:
                target = self._pick_downgrade(parts)
                if target is not None:
                    await self._downgrade(target)
                    used = self._refresh(parts)

        if used > budget:
            raise ContextOverflowError(
                f"上下文降级链用尽仍超预算（{used} > {budget}），请开启新会话")

        # ---- 阶段 2：滞后带（超 85% 压到 70%，防阈值抖动） ----
        trigger = int(budget * cfg.compress_trigger)
        target_line = int(budget * cfg.compress_target)
        if used > trigger:
            guard = 0
            while used > target_line and guard < 4:
                guard += 1
                target = self._pick_downgrade(parts)
                if target is None:
                    break
                await self._downgrade(target)
                used = self._refresh(parts)

        out = self._assemble(parts)
        self._layout = {p.name: p.tokens for p in parts}
        self._layout["tools"] = tools_cost
        self._last_estimate = self.counter.estimate(out)
        return out

    def last_layout(self) -> dict[str, int]:
        """调试/trace：上轮各分区实际 token 占比。"""
        return dict(self._layout)

    def calibrate(self, reported_input_tokens: int) -> float:
        """Loop 拿真实 usage 回执对账（电表读数修正目测手感）。"""
        if self._last_estimate > 0:
            return self.counter.calibrate(reported_input_tokens,
                                          self._last_estimate)
        return self.counter.cjk_ratio

    # ---------- 内部：切分 / 计量 / 拼装 ----------

    def _split_recent(self, rest: list[Message]) -> tuple[list, list]:
        """按轮界（assistant-with-calls）切出最近 keep_recent_turns 轮。"""
        k = self.history.config.keep_recent_turns
        call_idx = [i for i, m in enumerate(rest)
                    if m.role == "assistant" and m.tool_calls]
        if not call_idx:
            return [], list(rest)
        cut = call_idx[-k] if len(call_idx) >= k else call_idx[0]
        return rest[cut:], rest[:cut]

    def _count(self, part: Partition) -> int:
        msgs = part.content
        return self.counter.estimate(msgs) if msgs else 0

    def _refresh(self, parts: list[Partition]) -> int:
        for p in parts:
            p.tokens = self._count(p)
        return sum(p.tokens for p in parts)

    def _assemble(self, parts: list[Partition]) -> list[Message]:
        """system → memory → history → latest（首尾高召回，中间低价值区）。"""
        out: list[Message] = []
        for p in parts:
            out.extend(p.content)
        # 压缩边界恰好切在 calls/tool 之间 → 重组后重跑配对检查（清洗头部孤悬）
        return HistoryManager._clean_head(out)

    # ---------- 内部：降级链 ----------

    def _pick_downgrade(self, parts: list[Partition]) -> Partition | None:
        """取还有降级余地的最高优先级分区（先压最不痛的）。"""
        cands = [p for p in parts if p.compressible
                 and self._next_level(p) is not None]
        if not cands:
            return None
        return max(cands, key=lambda p: p.priority)

    def _next_level(self, part: Partition) -> int | None:
        if part.name == "history":
            return part.level + 1 if part.level < 3 else None
        if part.name == "memory":
            return 1 if part.level == 0 else None
        return None

    async def _downgrade(self, part: Partition) -> None:
        nxt = self._next_level(part)
        if nxt is None:
            return
        part.level = nxt
        if part.name == "history":
            if nxt == 1:                   # A 档：旧 Observation 占位替换
                part.content = self.history.sweep_replace_observations(
                    part.content, keep_recent_turns=0)
            elif nxt == 2:                 # B 档：模板摘要（pin 原文保留）
                pinned = self._pinned_of(part)
                part.content = [self.compressor.template_digest(part.content),
                                *pinned]
            elif nxt == 3:                 # C 档：LLM 摘要（pin 原文保留）
                pinned = self._pinned_of(part)
                budget = max(400, self.counter.estimate(part.content) // 4)
                summary = await self.compressor.summarize(
                    [m for m in part.content if not self.history.is_pinned(m)],
                    budget=budget)
                part.content = [summary, *pinned]
        elif part.name == "memory":        # 记忆分区可整体丢（M08 兜底可召回）
            part.content = []

    def _pinned_of(self, part: Partition) -> list[Message]:
        """pinned 消息压缩后原文保留（用户约定 50 轮后仍被遵守）。"""
        return [m for m in part.content if self.history.is_pinned(m)]

    def _truncate_latest(self, part: Partition, latest_budget: int) -> None:
        """最后兜底：latest 里的肥 tool 消息逐个 L1 截断（保 tool_call_id）。"""
        if latest_budget <= 0 or not part.content:
            return
        per_msg = max(latest_budget // max(
            sum(1 for m in part.content if m.role == "tool"), 1), 500)
        out: list[Message] = []
        for m in part.content:
            if m.role == "tool" and m.content and len(m.content) > per_msg:
                out.append(replace(m, content=self.truncator.truncate(
                    m.content, budget=per_msg)))
            else:
                out.append(m)
        part.content = out


# HistoryConfig 重导出（builder 的常用伴手）
__all__ = ["BudgetConfig", "ContextBuilder", "ContextOverflowError",
           "HistoryConfig", "Partition"]
