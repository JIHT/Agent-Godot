"""voice/metrics.py —— 语音可观测埋点（M16 §9.2 缺口 14）

埋 5 个指标（§9.5）：
- **RTF**（Real-Time Factor）：转写耗时 / 音频时长。<1 表示比实时快
- **TTFA**（Time To First Audio）：合成请求 → 首块音频可播，实时链路唯一 KPI
- **VAD 裁剪率**：`1 - duration_after_vad / duration`，衡量省了多少算力
- **幻觉率**：疑似幻觉段 + 低置信词 的占比
- **CER**：有标注时（lab/m16 标注集）的字错率

设计：零依赖内存记录器 + 可选 sink 回调。M21 的 observability 接进来时
只需注册一个 sink（`add_sink`），把记录转发到真正的指标后端。
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = ["VoiceMetrics", "record", "get_metrics", "add_sink",
           "ttfa_timer", "cer"]

Sink = Callable[[str, dict[str, Any]], None]


@dataclass
class VoiceMetrics:
    records: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    sinks: list[Sink] = field(default_factory=list)
    max_records: int = 1000

    def record(self, kind: str, data: dict[str, Any] | None = None) -> None:
        payload = dict(data or {})
        self.counters[kind] += 1
        if len(self.records) < self.max_records:
            self.records.append((kind, payload))
        for s in self.sinks:
            try:
                s(kind, payload)
            except Exception as e:                     # noqa: BLE001
                logger.debug("metrics sink 失败: %s", e)

    def add_sink(self, sink: Sink) -> None:
        self.sinks.append(sink)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [d for k, d in self.records if k == kind]

    def summary(self) -> dict[str, Any]:
        """聚合摘要：均值类指标取平均，其余给计数。"""
        out: dict[str, Any] = {"counts": dict(self.counters)}
        for key in ("rtf", "ttfa_ms", "vad_trim_rate", "hallucination_rate", "cer"):
            vals = [d[key] for d in self.of("transcribe") + self.of("tts")
                    if isinstance(d.get(key), (int, float))]
            if vals:
                out[f"avg_{key}"] = sum(vals) / len(vals)
                out[f"max_{key}"] = max(vals)
        return out

    def reset(self) -> None:
        self.records.clear()
        self.counters.clear()


_METRICS = VoiceMetrics()


def get_metrics() -> VoiceMetrics:
    return _METRICS


def record(kind: str, data: dict[str, Any] | None = None) -> None:
    _METRICS.record(kind, data)


def add_sink(sink: Sink) -> None:
    _METRICS.add_sink(sink)


@contextmanager
def ttfa_timer(**extra: Any):
    """首音时延计时器：`with ttfa_timer(voice=...) as t: ...`。

    约定用法——在第一块音频产出时调用 `t.mark()`，退出上下文时自动埋点。
    """
    state = {"ttfa_ms": None, "start": time.perf_counter()}

    def mark() -> None:
        if state["ttfa_ms"] is None:
            state["ttfa_ms"] = (time.perf_counter() - state["start"]) * 1000

    state["mark"] = mark                               # type: ignore[assignment]
    try:
        yield state
    finally:
        total = (time.perf_counter() - state["start"]) * 1000
        record("tts", {"ttfa_ms": state["ttfa_ms"], "total_ms": total, **extra})


# ── CER 评估（§9.7 验收：CER < 8%，不是"目测"）──────────────────────────

def _levenshtein(a: list[str], b: list[str]) -> int:
    """标准编辑距离（零依赖；中文按字符，英文按词）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str, *, lang: str = "zh",
        normalize: bool = True) -> float:
    """字错率（中文，按字符）/ 词错率（英文，按空格分词）。

    ★ normalize=True 时先做归一，否则标点/全半角/数字写法差异会把指标
      污染得毫无意义（§9.2 缺口 7）。归一规则按语言不同：
    - 中文：去空白与标点、繁→简、ITN
    - 英文：小写、去标点但**保留空格**（空格是分词依据，删了就退化成 CER）
    """
    from .normalize import itn, normalize_for_match

    if normalize:
        r = itn(ref, lang)
        h = itn(hyp, lang)
        if lang.lower().startswith("zh"):
            r, h = normalize_for_match(r), normalize_for_match(h)
        else:
            import re
            r = re.sub(r"[^\w\s]", "", r).lower()
            h = re.sub(r"[^\w\s]", "", h).lower()
            r, h = re.sub(r"\s+", " ", r).strip(), re.sub(r"\s+", " ", h).strip()

    if lang.lower().startswith("zh"):
        seq_r, seq_h = list(r), list(h)
    else:
        seq_r, seq_h = r.split(), h.split()
    if not seq_r:
        return 0.0 if not seq_h else 1.0
    return _levenshtein(seq_r, seq_h) / len(seq_r)
