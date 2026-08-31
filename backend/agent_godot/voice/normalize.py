"""voice/normalize.py —— ITN / 标点 / PII / 匹配归一（M16 §9.2 缺口 4、15）

三个功能，三条不同的时机纪律（顺序错了指标就错）：

1. **ITN（逆文本归一化）**：「百分之三十」→「30%」、「二零二六年」→「2026年」
   → **必须在特征计算之前**做。因为它直接改变语速的**分子**（字数 5 → 3），
   不做的话不同音频之间的语速数值不可比（§1.2 ⑤）。

2. **标点恢复**：给不带标点的引擎按间隙补句末标点。标点不计入字数。

3. **PII 脱敏**：→ **必须在特征计算之后、落库之前**做。
   脱敏会改变文本长度，若在特征前做会污染语速/填充词统计。所以本模块
   提供的是 `redact_result()`（返回脱敏**副本**），由 tools/export 在持久化
   边界显式调用，而不是混进 `apply()`。

★ 精度优先于召回：中文数字转换只在**多字符数字串**（长度 ≥ 2）时触发。
  宁可漏转，也不要把「一样」「第一」「一直」里的「一」转成「1」——
  错转的代价（语速分子错 + LLM 读到怪文本）远大于漏转。
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace

from .config import NormalizeConfig
from .schema import Seg, TranscriptionResult, WordInfo

logger = logging.getLogger(__name__)

__all__ = ["itn", "cn2num", "restore_punctuation", "redact_pii",
           "redact_result", "normalize_for_match", "apply"]


# ── 中文数字 → 阿拉伯数字 ──────────────────────────────────────────────

_DIGITS = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2,
           "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6,
           "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9, "幺": 1}
_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_BIGS = {"万": 10 ** 4, "亿": 10 ** 8, "兆": 10 ** 12}
_NUM_CHARS = "".join(_DIGITS) + "".join(_UNITS) + "".join(_BIGS)

_NUM_RUN = re.compile(f"[{_NUM_CHARS}]{{2,}}")
_PERCENT = re.compile(f"百分之([{_NUM_CHARS}]+)")


def cn2num(s: str) -> int | None:
    """中文数字串 → 整数。无法解析返回 None。

    两套规则：
    - 含「十百千万亿」→ 按位值累加（二十三=23、一亿两千万=120000000）
    - 纯数字字符且长度 ≥ 4 → 逐位拼接（二零二六=2026，年份/编号的读法）

    长度 < 2 的串不与转换（精度优先：避开「一样」「第一」这类词）。
    """
    if not s or len(s) < 2 or any(c not in _NUM_CHARS for c in s):
        return None

    if not any(c in _UNITS or c in _BIGS for c in s):
        if len(s) >= 4 and all(c in _DIGITS for c in s):
            return int("".join(str(_DIGITS[c]) for c in s))
        return None

    total = 0        # 最终结果
    section = 0      # 当前「亿/万」段内累计
    cur = 0          # 当前读到的数字
    for ch in s:
        if ch in _DIGITS:
            cur = _DIGITS[ch]
        elif ch in _UNITS:
            section += (cur if cur else 1) * _UNITS[ch]
            cur = 0
        elif ch in _BIGS:
            section += cur
            total += section * _BIGS[ch]
            section, cur = 0, 0
    return total + section + cur


def itn(text: str, lang: str = "zh") -> str:
    """逆文本归一化。非中文原样返回（英文 ITN 需要更复杂的规则集，暂不实现）。"""
    if not text or not lang.lower().startswith("zh"):
        return text

    # 百分之三十 → 30%（优先于通用规则，否则会变成「百分之30」）
    def _pct(m: re.Match[str]) -> str:
        n = cn2num(m.group(1))
        return f"{n}%" if n is not None else m.group(0)
    text = _PERCENT.sub(_pct, text)

    def _num(m: re.Match[str]) -> str:
        n = cn2num(m.group(0))
        return str(n) if n is not None else m.group(0)
    return _NUM_RUN.sub(_num, text)


# ── 标点恢复 ──────────────────────────────────────────────────────────

_SENT_END = "。！？!?"
_CLAUSE_END = "，,、；;:："


def restore_punctuation(tr: TranscriptionResult, gap_s: float = 0.6) -> TranscriptionResult:
    """按词间间隙补标点：> gap_s 补句末，> gap_s/2 补句内停顿。

    只给**不带标点**的引擎用（whisper 自带标点，配了会重复）。
    标点不进字数统计（features.count_units 会跳过），故不影响语速。
    """
    words = tr.words
    for a, b in zip(words, words[1:]):
        if not a.text.strip():
            continue
        last = a.text.strip()[-1]
        gap = b.start - a.end
        if gap >= gap_s and last not in _SENT_END:
            a.text = a.text.rstrip() + "。"
        elif gap >= gap_s / 2 and last not in _SENT_END + _CLAUSE_END:
            a.text = a.text.rstrip() + "，"
    _rebuild_segments(tr)
    return tr


# ── PII 脱敏 ──────────────────────────────────────────────────────────

_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[邮箱]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "[密钥]"),
    (re.compile(r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{16,}\b"), "[密钥]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
]


def redact_pii(text: str) -> str:
    """脱敏：邮箱 / 手机号 / 身份证 / API key / IPv4。"""
    for pat, repl in _PII_PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact_result(tr: TranscriptionResult,
                  cfg: NormalizeConfig | None = None) -> TranscriptionResult:
    """返回**脱敏副本**（不改原对象）。

    ★ 调用时机：特征算完之后、落库/导出/送 LLM 之前。
      在特征之前脱敏会改变文本长度 → 污染语速与填充词统计。
    """
    from .config import NormalizeConfig as _NC
    cfg = cfg or _NC()
    if not cfg.redact_pii:
        return tr

    out = TranscriptionResult(
        language=tr.language, duration=tr.duration,
        duration_after_vad=tr.duration_after_vad,
        segments=[], provenance=replace(tr.provenance))
    for s in tr.segments:
        out.segments.append(Seg(
            start=s.start, end=s.end, text=redact_pii(s.text),
            words=[WordInfo(text=redact_pii(w.text), start=w.start, end=w.end,
                            prob=w.prob, speaker=w.speaker) for w in s.words],
            speaker=s.speaker, emotion=s.emotion, events=list(s.events),
            avg_logprob=s.avg_logprob, compression_ratio=s.compression_ratio,
            no_speech_prob=s.no_speech_prob))
    return out


# ── 匹配归一（填充词统计用）────────────────────────────────────────────

_T2S = {"這個": "这个", "那麼": "那么", "什麼": "什么", "怎麼": "怎么",
        "因為": "因为", "所以": "所以", "裡面": "里面", "時候": "时候",
        "這樣": "这样", "一樣": "一样", "們": "们", "來": "来", "個": "个"}
_STRIP = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’()（）…—\-]")


def normalize_for_match(s: str) -> str:
    """填充词匹配前归一：去空白标点 + 大小写 + 常见繁→简。"""
    for k, v in _T2S.items():
        s = s.replace(k, v)
    return _STRIP.sub("", s).lower()


# ── 编排 ──────────────────────────────────────────────────────────────

def _itn_spans(text: str, lang: str) -> list[tuple[int, int, str]]:
    """找出所有 ITN 替换区间 [(start, end, replacement), ...]（互不重叠）。"""
    spans: list[tuple[int, int, str]] = []
    taken: set[int] = set()

    for m in _PERCENT.finditer(text):           # 百分之X → X%（优先）
        n = cn2num(m.group(1))
        if n is None:
            continue
        spans.append((m.start(), m.end(), f"{n}%"))
        taken.update(range(m.start(), m.end()))

    for m in _NUM_RUN.finditer(text):           # 其余数字串
        if any(i in taken for i in range(m.start(), m.end())):
            continue
        n = cn2num(m.group(0))
        if n is not None:
            spans.append((m.start(), m.end(), str(n)))
            taken.update(range(m.start(), m.end()))
    return spans


def apply_itn_to_segment(seg: Seg, lang: str) -> None:
    """★ 在**段的字符序列**上做 ITN，而不是逐词做。

    为什么必须这样：中文的词级时间戳是**逐字符**的（whisper 与强制对齐都
    如此），「百分之三十」是 5 个独立 word。逐词调用 itn() 时每个词只有
    一个字 → 长度 < 2 → 永不转换，ITN 形同虚设。

    做法：把段内字符摊平成带时间戳的序列 → 在整串上跑 ITN 拿到替换区间 →
    把区间内的字符**合并成一个新 token**，时间范围取源字符的 min/max。
    这样既改对了字数（5 → 3），又保住了时间戳。
    """
    if not seg.words:
        seg.text = itn(seg.text, lang)
        return

    flat: list[tuple[str, float, float, float, str | None]] = []
    for w in seg.words:
        for c in w.text:
            flat.append((c, w.start, w.end, w.prob, w.speaker))
    text = "".join(c for c, *_ in flat)
    spans = _itn_spans(text, lang)
    if not spans:
        return

    new_words: list[WordInfo] = []
    i = 0
    for s, e, repl in sorted(spans):
        while i < s:                            # 区间前的原样保留
            c, st, en, pr, sp = flat[i]
            new_words.append(WordInfo(text=c, start=st, end=en, prob=pr, speaker=sp))
            i += 1
        chunk = flat[s:e]                       # 区间内的字符合并成一个 token
        new_words.append(WordInfo(
            text=repl,
            start=min(f[1] for f in chunk),
            end=max(f[2] for f in chunk),
            prob=min(f[3] for f in chunk),
            speaker=chunk[0][4]))
        i = e
    while i < len(flat):
        c, st, en, pr, sp = flat[i]
        new_words.append(WordInfo(text=c, start=st, end=en, prob=pr, speaker=sp))
        i += 1

    seg.words = new_words
    seg.text = "".join(w.text for w in new_words)
    if new_words:
        seg.start, seg.end = new_words[0].start, new_words[-1].end


def apply(tr: TranscriptionResult, cfg: NormalizeConfig | None = None,
          feat_cfg: object | None = None) -> TranscriptionResult:
    """在 TranscriptionResult 上施加 ITN + 标点恢复（**不含脱敏**）。

    就地修改并返回。
    """
    from .config import NormalizeConfig as _NC
    cfg = cfg or _NC()
    lang = tr.language or tr.provenance.language or ""

    if cfg.itn:
        for seg in tr.segments:
            apply_itn_to_segment(seg, lang)
    if cfg.restore_punctuation:
        restore_punctuation(tr, cfg.punc_gap_s)
    else:
        _rebuild_segments(tr)

    tr.provenance.normalized = True
    return tr


def _rebuild_segments(tr: TranscriptionResult) -> None:
    """段文本由词重建，段边界对齐首尾词（与 faster-whisper 的行为一致）。"""
    for seg in tr.segments:
        if seg.words:
            seg.text = "".join(w.text for w in seg.words)
            seg.start, seg.end = seg.words[0].start, seg.words[-1].end
