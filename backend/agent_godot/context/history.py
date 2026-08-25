"""context/history.py —— 滚动窗口 + 保留配对 + pin（M07 §1.3 / §4 步骤 2）

协议铁律：assistant(tool_calls) 与 tool 消息必须成对——滑窗从中间切开
（切在 calls 与 result 之间），下一轮请求直接 400。收集算法以"对"为单位：
从最新往回扫，pending_calls 集合跟踪尚未闭环的 call_id，配对完整性 > 预算上限。

大白话：撕票据要连存根——发票（tool_calls）和回执（tool 结果）是一对，
只留其一会计（API）直接打回。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from ..core import Message
from .token_counter import TokenCounter


@dataclass
class HistoryConfig:
    max_history_tokens: int = 64_000
    keep_recent_turns: int = 3
    pinned_max: int = 32


class HistoryManager:
    """会话历史的滚动治理：滑窗（保留配对）/ pin（关键消息钉住）/ sweep（A 档占位）。"""

    def __init__(self, counter: TokenCounter | None = None,
                 config: HistoryConfig | None = None):
        self.counter = counter or TokenCounter()
        self.config = config or HistoryConfig()
        self._pinned: list[int] = []       # id() 的 LRU 序列（Message 不可变，按身份钉）

    # ---------- pin：钉在软木板上，永不滚出 ----------

    def pin(self, target: "int | Message",
            messages: list[Message] | None = None) -> Message:
        """把关键消息（用户约定/重大决策）钉住：rolling 与压缩都跳过 pinned。

        按索引 pin 时必须提供消息列表（索引无独立上下文）。
        上限 pinned_max 条，超限 LRU 淘汰最旧。
        """
        if isinstance(target, int):
            if messages is None:
                raise ValueError("按索引 pin 需要同时提供 messages 列表")
            msg = messages[target]
        else:
            msg = target
        key = id(msg)
        if key in self._pinned:
            self._pinned.remove(key)       # 重新钉 = 提到 LRU 最新位
        self._pinned.append(key)
        if len(self._pinned) > self.config.pinned_max:
            self._pinned.pop(0)
        return msg

    def unpin(self, msg: Message) -> None:
        key = id(msg)
        if key in self._pinned:
            self._pinned.remove(key)

    def is_pinned(self, msg: Message) -> bool:
        return id(msg) in self._pinned

    @property
    def pinned_count(self) -> int:
        return len(self._pinned)

    # ---------- 滚动窗口：保留配对 ----------

    def rolling(self, messages: list[Message],
                max_tokens: int | None = None) -> list[Message]:
        """保留配对滑窗：从新到旧收集，pending_calls 未清空前不许 break。

        倒序遍历的方向性：tool 结果先到、发起的 assistant 后到——
        tool 把 call_id 记为"欠条"（pending.add），assistant 到达才勾销
        （pending.discard）。pending 非空 = 有结果还欠着发起方，不许停。

        pinned 消息即使落在窗口外也要捞回（钉在软木板上的永不滚出）；
        捞回可能撕开新的配对缺口 → _ensure_pairing 双向修补至收敛。
        """
        limit = self.config.max_history_tokens if max_tokens is None else max_tokens
        n = len(messages)
        included: set[int] = set()
        used = 0
        pending: set[str] = set()          # 已收下结果、还缺发起方 assistant 的 call_id
        for i in range(n - 1, -1, -1):
            m = messages[i]
            cost = self.counter.estimate([m])
            newest = i == n - 1
            if (used + cost > limit and not pending
                    and not self.is_pinned(m) and not newest):
                break                      # 最新一条永远保留（空窗口无意义）
            if m.role == "tool" and m.tool_call_id:
                pending.add(m.tool_call_id)        # 回执先到，欠一张发票
            elif m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    pending.discard(tc.id)         # 发票到了，欠条勾销
            included.add(i)
            used += cost                   # 配对优先于预算（临时超一点）
        # 捞回落在窗口外的 pinned（按原顺序归位）
        included |= {i for i, m in enumerate(messages) if self.is_pinned(m)}
        # 捞回/截断可能撕开配对缺口 → 双向修补（收敛上限 3 轮足够）
        self._ensure_pairing(messages, included)
        out = [messages[i] for i in sorted(included)]
        return self._clean_head(out)

    @staticmethod
    def _ensure_pairing(messages: list[Message], included: set[int]) -> None:
        """修补配对：included 里的 assistant(calls) 拉入其 tool 结果，tool 拉入其发起方。"""
        for _ in range(3):
            changed = False
            call_ids: set[str] = set()
            for i in included:
                m = messages[i]
                if m.role == "assistant" and m.tool_calls:
                    call_ids.update(tc.id for tc in m.tool_calls)
            for j, m in enumerate(messages):
                if (m.role == "tool" and m.tool_call_id in call_ids
                        and j not in included):
                    included.add(j)
                    changed = True
            need = {messages[j].tool_call_id for j in included
                    if messages[j].role == "tool"}
            for i, m in enumerate(messages):
                if (m.role == "assistant" and m.tool_calls
                        and any(tc.id in need for tc in m.tool_calls)
                        and i not in included):
                    included.add(i)
                    changed = True
            if not changed:
                break

    @staticmethod
    def _clean_head(msgs: list[Message]) -> list[Message]:
        """清洗头部孤悬 tool：首条 tool 消息若无前文 assistant 发起过该调用则丢弃。

        有些 API 首条消息是 tool 直接拒收；pinned 的 tool 消息其配对 assistant
        被滚出时也会产生孤悬——这里统一兜底（可能级联，用 while）。
        """
        out = list(msgs)
        while out and out[0].role == "tool":
            cid = out[0].tool_call_id
            has_caller = any(
                m.role == "assistant" and m.tool_calls
                and any(tc.id == cid for tc in m.tool_calls)
                for m in out)
            if has_caller:
                break
            out.pop(0)
        return out

    # ---------- sweep：A 档删除法（旧 Observation 占位替换） ----------

    def sweep_replace_observations(self, messages: list[Message],
                                   keep_recent_turns: int | None = None
                                   ) -> list[Message]:
        """旧 tool 消息 shrink 成一行占位（信息价值随轮次衰减 100 倍）。

        保护最近 keep_recent_turns 轮（assistant-with-calls 为轮界）与 pinned；
        占位必须保留 tool_call_id——配对键丢了比内容丢了更致命。
        """
        n = (self.config.keep_recent_turns if keep_recent_turns is None
             else keep_recent_turns)
        call_idx = [i for i, m in enumerate(messages)
                    if m.role == "assistant" and m.tool_calls]
        if n <= 0 or not call_idx:
            protect_from = len(messages) if n <= 0 else 0
        else:
            protect_from = call_idx[-n] if len(call_idx) >= n else call_idx[0]
        names = {tc.id: tc.name
                 for m in messages if m.role == "assistant" and m.tool_calls
                 for tc in m.tool_calls}
        out: list[Message] = []
        for i, m in enumerate(messages):
            if (m.role == "tool" and i < protect_from and not self.is_pinned(m)):
                label = names.get(m.tool_call_id or "", "tool")
                size = self.counter.estimate_text(m.content)
                out.append(replace(
                    m,
                    content=f'<observation tool="{label}" summarized=true '
                            f'tokens={size}/>'))
            else:
                out.append(m)
        return out
