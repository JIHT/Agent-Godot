"""context/truncator.py —— 工具结果分级截断（M07 §1.4 / §4 步骤 3）

三级策略：
- L1 展示截断（truncate）：进上下文前截头保尾，保结构边界（JSON 截两半
  比超长更糟——模型会尝试续写残缺 JSON）；
- L2 结构化摘要（summarize_struct）：散装零件装收纳盒——列表→计数+前 N 项，
  日志→错误行提取，JSON→顶层键骨架；
- L3 事后占位（HistoryManager.sweep_replace_observations，A 档共用）。
"""
from __future__ import annotations

import json
import re

from ..tools.sandbox import truncate as head_tail_truncate

_ERROR_LINE = re.compile(r"\b(ERROR|WARN|FATAL|Traceback|失败|异常|错误)\b", re.I)


class ObservationTruncator:
    """工具结果（上下文最肥杀手）的进上下文前治理。"""

    def __init__(self, default_budget: int = 2000):
        self.default_budget = default_budget

    # ---------- L1：展示截断 ----------

    def truncate(self, content: str | None, budget: int | None = None) -> str:
        """保头保尾（错误信息常在尾部 traceback）+ JSON 结构边界保护。"""
        budget = self.default_budget if budget is None else budget
        if not content or len(content) <= budget:
            return content or ""
        stripped = content.strip()
        # 结构边界保护：合法 JSON 不做拦腰截断，改走结构化摘要
        if stripped[:1] in "{[":
            try:
                json.loads(stripped)
                return self.summarize_struct("json", stripped)
            except (json.JSONDecodeError, ValueError):
                pass                       # 不是合法 JSON → 正常截断
        head = int(budget * 0.75)
        tail = max(int(budget * 0.15), 40)
        return head_tail_truncate(content, head=head, tail=tail)

    # ---------- L2：结构化摘要 ----------

    def summarize_struct(self, tool: str, raw: str | None) -> str:
        """按内容形态分支：紧凑但不丢骨架。"""
        s = (raw or "").strip()
        if not s:
            return ""
        # ① JSON 列表：计数 + 前 N 项
        try:
            data = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, list):
            preview = " | ".join(str(x)[:60] for x in data[:20])
            return f"共 {len(data)} 项（{tool}），前 20 项: {preview}"
        # ② JSON 对象：顶层键骨架
        if isinstance(data, dict):
            keys = list(data)[:30]
            return (f"JSON 对象（{tool}），顶层键 {len(data)} 个: "
                    f"{keys}")
        # ③ 多行文本（目录列表/日志）：错误行提取优先于行数摘要
        lines = s.splitlines()
        errs = [ln.strip()[:120] for ln in lines if _ERROR_LINE.search(ln)]
        if errs:
            return (f"{len(lines)} 行中 {len(errs)} 条错误/警告（{tool}）: "
                    + " / ".join(errs[:10]))
        if len(lines) >= 20:
            preview = " | ".join(ln.strip()[:80] for ln in lines[:20])
            return f"共 {len(lines)} 行（{tool}），前 20 行: {preview}"
        # ④ 兜底 L1
        return self.truncate(s, self.default_budget)
