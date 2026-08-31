"""特征指标测试（M16 §1.2 · §9.6）

语速的两条纪律：
1. 分母 = 首尾词跨度 − 长间隙（不是"音频总长 − 停顿"）
2. 分子 = 中文数字 / 英文数词（混算出鬼数）
"""
from __future__ import annotations

import pytest

from agent_godot.voice.config import FeaturesConfig
from agent_godot.voice.features import (count_fillers, count_units, extract_features,
                                        pauses, rhythm_variance, speech_rate,
                                        to_diagnosis_input)
from agent_godot.voice.schema import WordInfo


def _words(text: str, start: float = 0.0, step: float = 0.5,
           dur: float = 0.2, pause_after: int | None = None,
           pause_s: float = 0.0) -> list[WordInfo]:
    ws, t = [], start
    for i, c in enumerate(text):
        ws.append(WordInfo(text=c, start=round(t, 3), end=round(t + dur, 3)))
        t += step
        if pause_after and (i + 1) % pause_after == 0:
            t += pause_s
    return ws


# ── 语速分母 ──────────────────────────────────────────────────────────

def test_speech_rate_excludes_pauses():
    """60s 音频、100 字、10s 静音 → 120 字/分。

    ★ 关键在于：静音在**中间**和静音在**开头/结尾**必须得到同一个数。
      旧公式（总长 − 停顿）只在静音位于中间时正确。
    """
    # 静音在中间：分两段，中间空 10s
    ws = _words("字" * 50, start=0.0, step=0.5)
    ws += _words("字" * 50, start=ws[-1].end + 10.0, step=0.5)
    rate_mid, eff_mid = speech_rate(ws, "zh")

    # 静音在开头：整体后移 10s
    ws2 = _words("字" * 100, start=10.0, step=0.5)
    rate_head, eff_head = speech_rate(ws2, "zh")

    assert rate_mid == pytest.approx(120.0, rel=0.02)
    assert rate_head == pytest.approx(120.0, rel=0.02)
    assert eff_mid == pytest.approx(eff_head, rel=0.02)
    # 旧公式在"静音在开头"时会算成 100 字/分，差 30%
    assert rate_head != pytest.approx(100.0, rel=0.05)


def test_speech_rate_ignores_leading_silence():
    """开头愣了 10 秒：有效说话时长只有 50s，正确答案是 150 字/分。

    这正是 §5 原测试隐藏的 bug（它假设静音一定在词之间）。
    """
    ws = _words("字" * 100, start=10.0, step=0.4)   # 100 字 × 0.4s ≈ 40s
    rate, eff = speech_rate(ws, "zh")
    assert eff == pytest.approx(40.0, rel=0.05)
    assert rate == pytest.approx(100 / (40 / 60), rel=0.05)


def test_speech_rate_lower_bound_protection():
    """除零/负数防护：单字、零跨度都不炸。"""
    assert speech_rate([], "zh")[0] == 0.0
    assert speech_rate([WordInfo("a", 1.0, 1.0)], "zh")[0] == 0.0


# ── 语速分子：单位 ────────────────────────────────────────────────────

def test_units_follow_detected_language():
    """★ 混算出鬼数：同一段中文，按"字"是 12，按"词"（碎片数）只有 3。

    whisper 的中文词级时间戳是**多字符碎片**（中文无空格），若照搬英文的
    "数词"口径，3 个碎片配上英文阈值（130~170 词/分）→ 报告"疑似表达障碍"。
    """
    # 三个碎片："大家好" / "我叫小明" / "我做过项目"，共 12 个汉字
    fragments = [WordInfo("大家好", 0.0, 0.6),
                 WordInfo("我叫小明", 0.8, 1.6),
                 WordInfo("我做过项目", 1.8, 2.8)]

    assert count_units(fragments, "zh") == pytest.approx(12.0)   # 数字符 ✓
    assert count_units(fragments, "en") == pytest.approx(3.0)    # 数碎片 → 鬼数

    r_zh, _ = speech_rate(fragments, "zh")
    r_en, _ = speech_rate(fragments, "en")
    assert r_zh == pytest.approx(r_en * 4, rel=0.01)


def test_count_units_latin_run_weighed_once():
    """连续拉丁字母（一个英文单词）合计 en_weight，**不逐字母累加**。

    "Godot" 是 2 个音节；按 5 字母 × 1.5 = 7.5 会严重高估。
    """
    ws = [WordInfo("你", 0.0, 0.2), WordInfo("好", 0.3, 0.5),
          WordInfo("，", 0.5, 0.6), WordInfo("Godot", 0.7, 1.0)]
    assert count_units(ws, "zh", en_weight=1.5) == pytest.approx(2 + 1.5)


def test_count_units_digits_weigh_one_each():
    """数字每个计 1.0："2026" 读作"二零二六"，4 个字符就是 4 个音节。"""
    ws = [WordInfo("2", 0.0, 0.1), WordInfo("0", 0.1, 0.2),
          WordInfo("2", 0.2, 0.3), WordInfo("6", 0.3, 0.4),
          WordInfo("年", 0.4, 0.5)]
    assert count_units(ws, "zh") == pytest.approx(5.0)


def test_rate_verdict_uses_language_specific_thresholds():
    """200 在中文是"正常"，在英文是"偏快"——阈值表必须按语言分开。"""
    ws = _words("字" * 60, step=0.3)               # 60 字 / 18s ≈ 200 字/分
    f_zh = extract_features(_tr_of(ws, "zh"), FeaturesConfig())
    f_en = extract_features(_tr_of(ws, "en"), FeaturesConfig())

    assert f_zh.rate_unit == "字/分"
    assert f_en.rate_unit == "词/分"
    assert f_zh.rate_verdict == "正常"
    assert f_en.rate_verdict in ("偏快", "略快")


def _tr_of(words, lang):
    from agent_godot.voice.schema import (Provenance, Seg,
                                          TranscriptionResult)
    return TranscriptionResult(
        language=lang, duration=words[-1].end + 1.0,
        segments=[Seg(words[0].start, words[-1].end,
                      "".join(w.text for w in words), words=words)],
        provenance=Provenance(engine="test", language=lang, language_prob=1.0))


# ── 停顿 ──────────────────────────────────────────────────────────────

def test_pause_kinds_threshold():
    """0.8~2s → 思考；>2s → 卡壳；恰好 0.8s 计入（>=）；0.79s 不计。"""
    def mk(gap):
        return [WordInfo("a", 0.0, 0.2), WordInfo("b", 0.2 + gap, 0.4 + gap)]

    assert pauses(mk(0.79), 0.8, 2.0) == []
    assert len(pauses(mk(0.8), 0.8, 2.0)) == 1
    assert pauses(mk(0.8), 0.8, 2.0)[0].kind == "思考"
    assert pauses(mk(2.0), 0.8, 2.0)[0].kind == "思考"     # 边界：不超过 2s
    assert pauses(mk(2.1), 0.8, 2.0)[0].kind == "卡壳"


# ── 填充词 ────────────────────────────────────────────────────────────

def test_filler_count_normalization():
    """「就是就是」计 2 次（重叠扫描，非贪心）。"""
    ws = _words("就是就是我们去吧", step=0.3)
    hits = count_fillers(ws, ["就是"], "zh")
    assert hits["就是"] == 2


def test_filler_quote_exempt():
    """引述语境豁免：「他说'那个'一词」不算填充词。"""
    ws = _words("他说“那个”一词", step=0.3)
    assert count_fillers(ws, ["那个"], "zh", quote_exempt=True) == {}
    # 关掉豁免才计
    assert count_fillers(ws, ["那个"], "zh", quote_exempt=False)["那个"] == 1


def test_filler_english_word_match():
    """英文按词匹配（不是按字符重叠扫）。"""
    ws = [WordInfo("like", 0.0, 0.2), WordInfo("this", 0.3, 0.5),
          WordInfo("um", 0.6, 0.8)]
    hits = count_fillers(ws, ["like", "um", "uh"], "en")
    assert hits == {"like": 1, "um": 1}


# ── 节奏与诊断输入 ────────────────────────────────────────────────────

def test_rhythm_variance_zero_for_steady_speech():
    """匀速说话 → 方差接近 0；忽快忽慢 → 方差大。"""
    steady = _words("字" * 30, step=0.4)
    assert rhythm_variance(steady, 10) < 1.0

    jitter, t = [], 0.0
    for i in range(30):
        step = 0.2 if i % 2 else 0.8
        jitter.append(WordInfo("字", t, t + 0.1))
        t += step
    assert rhythm_variance(jitter, 10) > rhythm_variance(steady, 10)


def test_diagnosis_input_carries_ref_and_provenance(mock_tr):
    """LLM 输入必须带**参考区间 + 溯源**——否则论断无法复核（§1.3）。"""
    cfg = FeaturesConfig()
    f = extract_features(mock_tr, cfg)
    d = to_diagnosis_input(f, mock_tr, cfg)

    assert d["语速"]["参考区间"]["正常"] == [180.0, 240.0]
    assert d["语速"]["单位"] == "字/分"
    assert d["溯源"]["引擎"] == "mock"
    assert "情感分布" not in d                  # 无情感标签时不塞空字段


def test_low_confidence_propagates_to_features(mock_tr):
    """低置信转写 → 特征里标注（报告应写"数值仅供参考"）。"""
    mock_tr.segments[0].compression_ratio = 3.0     # > 2.4 → 疑似幻觉
    f = extract_features(mock_tr, FeaturesConfig())
    assert f.low_confidence is True
    assert to_diagnosis_input(f)["置信度"].startswith("低")
