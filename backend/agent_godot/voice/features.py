"""voice/features.py —— 时间戳 → 诊断指标（M16 §1.2）

三类指标，全部从词级时间戳推导，**零额外模型**：
- **流利度**：语速 / 停顿（思考型 vs 卡壳型）/ 节奏方差
- **填充词**：词表匹配 + 归一化 + 引述豁免
- **结构**：STAR / 要点覆盖（LLM 读转写判，规则判不了 → 见 diagnose.py）

方法论三步（可迁移到任何领域，§1.2 ②）：
**原始信号 → 可计算指标 → 业务化解释**。
"182 字/分"必须翻译成"语速偏快，建议 150~170"——阈值来自领域知识且进配置。

★ 两条决定数值正确性的实现纪律（§1.2 ⑤ + §9）：

1. **语速分母 = 首尾词跨度 − 所有超阈值间隙**，不是"音频总长 − 停顿"。
   后者漏算开头/结尾的静音：60s 音频、10s 静音在**开头**时，旧公式算得
   100 字/分，正确答案是 150 字/分——**同一个音频，静音位置不同就差 30%**。
   用跨度法则两种位置得到同一个数，且对 VAD 剪切天然免疫。

2. **分子按语言选单位**：中文数字、英文数词，混说按比例折算。
   混算出的数没法看（中文按"词"算会得 50 词/分 → 报告"疑似表达障碍"）。
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import asdict, dataclass, field
from typing import Literal

from .config import FeaturesConfig, RateRef
from .schema import TranscriptionResult, WordInfo

logger = logging.getLogger(__name__)

__all__ = ["Pause", "SpeechFeatures", "pauses", "count_units", "speech_rate",
           "count_fillers", "rhythm_variance", "extract_features",
           "to_diagnosis_input"]

_PUNCT = set("，。！？、；：\"'“”‘’()（）…—,.!?;:-—《》〈〉【】[]")
_QUOTES = set("\"'“”‘’「」『』")


@dataclass
class Pause:
    """一次停顿：起点、时长、类型。"""
    start: float
    duration: float
    kind: Literal["思考", "卡壳"]


@dataclass
class SpeechFeatures:
    speech_rate: float = 0.0
    rate_unit: str = "字/分"
    rate_ref: RateRef = field(default_factory=RateRef)
    rate_verdict: str = ""                 # 偏慢 / 正常 / 偏快
    pauses: list[Pause] = field(default_factory=list)
    longest_pause: Pause | None = None
    total_pause_s: float = 0.0
    effective_speech_s: float = 0.0
    fillers: dict[str, int] = field(default_factory=dict)
    filler_count: int = 0
    filler_density: float = 0.0
    filler_verdict: str = ""
    rhythm_variance: float = 0.0
    total_speech_s: float = 0.0
    unit_count: float = 0.0
    low_confidence: bool = False
    emotions: dict[str, int] = field(default_factory=dict)
    speakers: list[str] = field(default_factory=list)


# ── 1. 停顿 ───────────────────────────────────────────────────────────

def pauses(words: list[WordInfo], threshold: float = 0.8,
           stuck: float = 2.0) -> list[Pause]:
    """相邻词间隙 ≥ threshold 的算停顿；> stuck 的算"卡壳"，其余"思考"。

    ★ 前提：words 的时间戳已还原到**原始音频轴**（§1.1 ⑤a）。未还原时
      所有间隙恒等于 2×speech_pad_ms，本函数直接失效（6.2s 卡壳会变成 0.6s）。
    """
    out: list[Pause] = []
    for a, b in zip(words, words[1:]):
        gap = b.start - a.end
        if gap >= threshold:
            out.append(Pause(start=a.end, duration=round(gap, 3),
                             kind="卡壳" if gap > stuck else "思考"))
    return out


# ── 2. 语速 ───────────────────────────────────────────────────────────

def _is_cjk(c: str) -> bool:
    o = ord(c)
    return 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF


def count_units(words: list[WordInfo], lang: str,
                en_weight: float = 1.5) -> float:
    """语速分子：中文数字、英文数词；混说片段按 en_weight 折算。

    中文口径下的三种字符：
    - **汉字 / 数字 / 符号** → 各计 1.0。数字按 1.0 是因为 "2026" 读作
      "二零二六"，4 个字符就是 4 个音节；"%"" 代表"百分之"也算一个单位。
    - **连续拉丁字母**（一个英文单词）→ 整段合计 en_weight，**不是逐字母**。
      "Godot" 是 2 个音节，按 5 个字母 × 1.5 = 7.5 会严重高估。
    - 空白与标点 → 不计。

    折算系数 1.5 的来源：中文正常 210 字/分 ÷ 英文正常 150 词/分 ≈ 1.4，
    取 1.5 并进配置（§9.5 的 en_word_to_zh_char）。
    """
    if lang.lower().startswith("en"):
        return float(sum(1 for w in words
                         if w.text.strip() and w.text.strip() not in _PUNCT))

    n = 0.0
    text = "".join(w.text for w in words)
    i = 0
    while i < len(text):
        c = text[i]
        if c in _PUNCT or c.isspace():
            i += 1
            continue
        if c.isascii() and c.isalpha():          # 连续拉丁字母视为一个词
            j = i
            while j < len(text) and text[j].isascii() and text[j].isalpha():
                j += 1
            n += en_weight
            i = j
            continue
        n += 1.0                                 # 汉字 / 数字 / 其他符号
        i += 1
    return n


def speech_rate(words: list[WordInfo], lang: str = "zh",
                pause_threshold: float = 0.8,
                en_weight: float = 1.5) -> tuple[float, float]:
    """返回 (语速, 有效说话时长秒)。

    分母 = 首词起点到尾词终点的跨度 − 所有超阈值间隙。
    **不引用音频总长** → 对 VAD 剪切/时间戳压缩天然免疫。
    """
    if len(words) < 2:
        return 0.0, 0.0
    span = words[-1].end - words[0].start
    if span <= 0:
        return 0.0, 0.0
    gaps = [b.start - a.end for a, b in zip(words, words[1:])]
    silent = sum(g for g in gaps if g >= pause_threshold)
    effective = max(span - silent, 1e-3)                # 下界保护，防负数/除零
    return count_units(words, lang, en_weight) / (effective / 60.0), effective


# ── 3. 填充词 ─────────────────────────────────────────────────────────

def count_fillers(words: list[WordInfo], filler_words: list[str],
                  lang: str = "zh", quote_exempt: bool = True) -> dict[str, int]:
    """统计填充词。

    三条纪律（§1.2 ⑤）：
    - **归一化**：繁简、大小写、全半角统一后再匹配
    - **连说分开计**：「就是就是」计 2 次（重叠扫描，非贪心）
    - **引述豁免**：「他说'那个'一词」不算（邻近有引号则跳过）
    """
    from .normalize import normalize_for_match

    if not filler_words:
        return {}

    norm_fillers = [(f, normalize_for_match(f)) for f in filler_words]
    norm_fillers = [(raw, n) for raw, n in norm_fillers if n]
    if not norm_fillers:
        return {}

    hits: dict[str, int] = {raw: 0 for raw, _ in norm_fillers}

    if lang.lower().startswith("en"):
        # 英文：按词匹配（大小写已归一）
        for w in words:
            t = normalize_for_match(w.text)
            for raw, n in norm_fillers:
                if t == n:
                    hits[raw] += 1
        return {k: v for k, v in hits.items() if v}

    # 中文：逐字符拼成平面串，做**重叠扫描**
    chars = [c for w in words for c in w.text if not c.isspace()]
    flat = "".join(normalize_for_match(c) for c in chars)
    for raw, n in norm_fillers:
        step = 1                                       # 允许重叠 → "就是就是"计 2
        for i in range(0, max(0, len(flat) - len(n) + 1), step):
            if flat[i: i + len(n)] == n:
                if quote_exempt and _near_quote(chars, i, len(n)):
                    continue
                hits[raw] += 1
    return {k: v for k, v in hits.items() if v}


def _near_quote(chars: list[str], start: int, length: int,
                window: int = 2) -> bool:
    """命中位置前后 window 个字符内是否出现引号（引述语境）。"""
    lo = max(0, start - window)
    hi = min(len(chars), start + length + window)
    return any(c in _QUOTES for c in chars[lo:hi])


# ── 4. 节奏方差 ───────────────────────────────────────────────────────

def rhythm_variance(words: list[WordInfo], window: int = 10) -> float:
    """滑动窗语速标准差：衡量"语速稳不稳"（忽快忽慢 = 方差大）。"""
    if len(words) < window or window < 2:
        return 0.0
    rates: list[float] = []
    for i in range(len(words) - window + 1):
        chunk = words[i: i + window]
        span = chunk[-1].end - chunk[0].start
        if span > 0:
            rates.append(len(chunk) / (span / 60.0))
    return float(statistics.pstdev(rates)) if len(rates) >= 2 else 0.0


# ── 编排 ──────────────────────────────────────────────────────────────

def _verdict(value: float, ref: RateRef) -> str:
    if value < ref.slow:
        return "偏慢"
    if value > ref.fast:
        return "偏快"
    lo, hi = ref.normal
    if value < lo:
        return "略慢"
    if value > hi:
        return "略快"
    return "正常"


def extract_features(tr: TranscriptionResult,
                     cfg: FeaturesConfig | None = None) -> SpeechFeatures:
    """转写结果 → 特征指标。这是 §1.2 的主入口。"""
    from .config import FeaturesConfig as _FC
    cfg = cfg or _FC()

    lang = tr.language or tr.provenance.language or "zh"
    words = tr.words

    ps = pauses(words, cfg.pause_thinking_s, cfg.pause_stuck_s)
    rate, effective = speech_rate(
        words, lang, cfg.pause_thinking_s, cfg.en_word_to_zh_char)
    units = count_units(words, lang, cfg.en_word_to_zh_char)

    fillers = count_fillers(words, cfg.filler_words, lang, cfg.quote_exempt)
    filler_total = sum(fillers.values())
    density = filler_total / max(units, 1.0) if units else 0.0

    lo, hi = cfg.filler_normal_rate
    if density > hi * 1.5:
        filler_verdict = "显著偏多"
    elif density > hi:
        filler_verdict = "偏多"
    elif density < lo:
        filler_verdict = "偏少"
    else:
        filler_verdict = "正常"

    ref = cfg.speech_rate_ref.get(lang[:2])
    if ref is None:
        ref = (cfg.speech_rate_ref.get("zh") or RateRef())

    emotions: dict[str, int] = {}
    for s in tr.segments:
        if s.emotion:
            emotions[s.emotion] = emotions.get(s.emotion, 0) + 1
    speakers = sorted({s.speaker for s in tr.segments if s.speaker})

    return SpeechFeatures(
        speech_rate=round(rate, 1),
        rate_unit=ref.unit,
        rate_ref=ref,
        rate_verdict=_verdict(rate, ref),
        pauses=ps,
        longest_pause=max(ps, key=lambda p: p.duration) if ps else None,
        total_pause_s=round(sum(p.duration for p in ps), 2),
        effective_speech_s=round(effective, 2),
        fillers=fillers,
        filler_count=filler_total,
        filler_density=round(density, 4),
        filler_verdict=filler_verdict,
        rhythm_variance=round(rhythm_variance(words, cfg.rhythm_window), 2),
        total_speech_s=round(tr.duration, 2),
        unit_count=units,
        low_confidence=tr.low_confidence,
        emotions=emotions,
        speakers=speakers,
    )


def to_diagnosis_input(f: SpeechFeatures, tr: TranscriptionResult | None = None,
                       cfg: FeaturesConfig | None = None) -> dict:
    """特征 → LLM 输入（结构化 + 附参考区间，让 LLM 的论断挂在可复核的数值上）。

    与 RAG 引用同构的信任机制（§1.2 ④）：报告说"语速过快"若不带实测值与
    参考区间，用户无法验证；带上后论断可复核，且实测值可回链到原始时间戳。
    """
    from .config import FeaturesConfig as _FC
    cfg = cfg or _FC()
    ref = f.rate_ref
    d: dict = {
        "语速": {"实测": f.speech_rate, "单位": f.rate_unit, "评价": f.rate_verdict,
                 "参考区间": {"偏慢": ref.slow, "正常": list(ref.normal),
                              "偏快": ref.fast}},
        "停顿": {"次数": len(f.pauses), "总时长_s": f.total_pause_s,
                 "卡壳次数(>2s)": sum(1 for p in f.pauses if p.kind == "卡壳"),
                 "最长停顿": ({"位置_s": f.longest_pause.start,
                               "时长_s": f.longest_pause.duration,
                               "类型": f.longest_pause.kind}
                              if f.longest_pause else None)},
        "填充词": {"总次数": f.filler_count, "密度": f.filler_density,
                   "评价": f.filler_verdict,
                   "正常区间": list(cfg.filler_normal_rate),
                   "明细": f.fillers},
        "节奏方差": f.rhythm_variance,
        "有效说话时长_s": f.effective_speech_s,
        "音频总时长_s": f.total_speech_s,
        "置信度": "低（数值仅供参考）" if f.low_confidence else "正常",
    }
    if f.emotions:
        d["情感分布"] = f.emotions
    if f.speakers:
        d["说话人"] = f.speakers
    if tr is not None:
        d["溯源"] = {"引擎": tr.provenance.engine,
                     "语言": tr.language,
                     "语言置信度": round(tr.provenance.language_prob, 3),
                     "已强制对齐": tr.provenance.aligned}
    return d
