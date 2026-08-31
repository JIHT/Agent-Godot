"""归一化 / ITN / PII / CER 测试（M16 §1.2 ⑤ · §9.2 缺口 4、7、15）"""
from __future__ import annotations

import pytest

from agent_godot.voice.features import count_units
from agent_godot.voice.metrics import cer
from agent_godot.voice.normalize import (cn2num, itn, normalize_for_match,
                                         redact_pii, redact_result,
                                         restore_punctuation)
from agent_godot.voice.schema import (Provenance, Seg, TranscriptionResult,
                                      WordInfo)


# ── ITN ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("src,want", [
    ("二十三", 23), ("一百二十三", 123), ("十", None),       # 长度<2 不转
    ("二零二六", 2026), ("一亿两千万", 120000000),
    ("一千零二十四", 1024), ("三十五", 35), ("两百", 200),
])
def test_cn2num(src, want):
    assert cn2num(src) == want


def test_itn_percent_and_year():
    assert itn("百分之三十", "zh") == "30%"
    assert itn("二零二六年", "zh") == "2026年"
    assert itn("一共一百二十三个人", "zh") == "一共123个人"


def test_itn_precision_over_recall():
    """宁可漏转，也不要把「一样」「第一」里的「一」转成「1」。

    错转的代价（语速分子错 + LLM 读到怪文本）远大于漏转。
    """
    assert itn("我们一样", "zh") == "我们一样"
    assert itn("第一", "zh") == "第一"
    assert itn("一直", "zh") == "一直"


def test_itn_changes_speech_rate_numerator():
    """★ ITN 直接改变语速分子：「百分之三十」5 个字 → 合并成「30%」一个 token。

    所以 ITN 必须在算特征**之前**做，且全文口径一致（否则不同音频不可比）。
    """
    from agent_godot.voice.normalize import apply_itn_to_segment
    from agent_godot.voice.schema import Seg

    seg = Seg(0.0, 0.0, "百分之三十",
              words=[WordInfo(c, i * 0.2, i * 0.2 + 0.1)
                     for i, c in enumerate("百分之三十")])
    assert count_units(seg.words, "zh") == 5.0

    apply_itn_to_segment(seg, "zh")
    assert seg.text == "30%"
    assert len(seg.words) == 1
    # 时间戳取源字符的 min/max，不丢也不飘
    assert seg.words[0].start == pytest.approx(0.0)
    assert seg.words[0].end == pytest.approx(0.9)
    assert count_units(seg.words, "zh") == pytest.approx(3.0)    # '3','0','%'


def test_itn_must_run_at_segment_level_not_per_word():
    """逐词调用 itn() 对中文无效——中文词级时间戳是**逐字符**的。

    单个字长度 < 2 → 永不转换，ITN 形同虚设。这就是必须有
    `apply_itn_to_segment`（在段字符序列上做）的原因。
    """
    assert itn("百", "zh") == "百"           # 单字不转（精度优先）
    assert itn("百分之三十", "zh") == "30%"  # 成串才转


def test_itn_skips_non_chinese():
    assert itn("twenty three", "en") == "twenty three"


# ── 标点恢复 ──────────────────────────────────────────────────────────

def test_restore_punctuation_by_gap():
    """按间隙补标点：>gap_s 补句末「。」，>gap_s/2 补句内「，」。"""
    tr = TranscriptionResult(
        language="zh", duration=10.0,
        segments=[Seg(0.0, 0.0, "你好我们开始吧",
                      words=[WordInfo("你", 0.0, 0.2), WordInfo("好", 0.3, 0.5),
                             WordInfo("我", 1.5, 1.7), WordInfo("们", 1.8, 2.0),
                             WordInfo("开", 2.4, 2.6), WordInfo("始", 2.7, 2.9),
                             WordInfo("吧", 3.0, 3.2)])])
    restore_punctuation(tr, gap_s=0.6)
    joined = "".join(w.text for w in tr.words)

    # 间隙 1.0s → 句末；间隙 0.4s → 句内
    assert joined == "你好。我们，开始吧"
    # 标点不进字数统计
    assert count_units(tr.words, "zh") == 7.0


# ── PII ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("src,token", [
    ("联系我 13812345678 谢谢", "[手机号]"),
    ("邮箱 a.b+c@x.com 收到", "[邮箱]"),
    ("密钥 sk-abcdefghijklmnopqrst 泄露", "[密钥]"),
    ("身份证 11010119900307123X 存档", "[身份证]"),
])
def test_redact_pii(src, token):
    assert token in redact_pii(src)


def test_redact_result_preserves_timestamps(mock_tr):
    """★ 脱敏只改文本，不动时间戳、不改词数——这是它与 ITN 的关键差别。

    ITN 在特征前做（改字数、影响语速），脱敏在特征后做（不能影响指标）。
    """
    tr = TranscriptionResult(
        language="zh", duration=5.0,
        segments=[Seg(0.0, 0.0, "电话13812345678",
                      words=[WordInfo("电", 0.0, 0.2), WordInfo("话", 0.3, 0.5),
                             WordInfo("13812345678", 0.6, 1.2)])])
    safe = redact_result(tr)

    assert "13812345678" not in safe.text
    assert "[手机号]" in safe.text
    # 词数与时间戳不变
    assert len(safe.words) == len(tr.words)
    assert [w.start for w in safe.words] == [w.start for w in tr.words]
    # 原对象未被就地修改（返回的是副本）
    assert "13812345678" in tr.text


def test_redact_result_noop_when_disabled(mock_tr):
    from agent_godot.voice.config import NormalizeConfig
    assert redact_result(mock_tr, NormalizeConfig(redact_pii=False)).text == mock_tr.text


# ── 匹配归一 ──────────────────────────────────────────────────────────

def test_normalize_for_match():
    assert normalize_for_match(" 就是， ") == "就是"
    assert normalize_for_match("LIKE") == "like"
    assert normalize_for_match("這個") == "这个"


# ── CER ───────────────────────────────────────────────────────────────

def test_cer_basic():
    """CER = 编辑距离 / 参考长度。"""
    assert cer("今天天气很好", "今天天气很好", lang="zh") == 0.0
    # 参考 6 字，漏 1 字 → 1/6
    assert cer("今天天气很好", "今天天气好", lang="zh") == pytest.approx(1 / 6)
    assert cer("abc", "abc", lang="en") == 0.0
    assert cer("hello world", "hello", lang="en") == pytest.approx(0.5)


def test_cer_normalizes_before_counting():
    """★ 不归一化的 CER 毫无意义：标点/数字写法差异会把指标污染掉。

    「百分之三十」与「30%」是同一句话的两种写法，归一化后 CER 应为 0。
    """
    assert cer("占比百分之三十", "占比30%", lang="zh") == 0.0
    assert cer("Hello, World!", "hello world", lang="en") == 0.0


def test_cer_regression_threshold():
    """§9.7 验收：自录标注集上中文 CER < 8%。

    这里用一段"人工标注 vs Mock 引擎输出"的对拍，断言不劣化。
    """
    ref = "大家好我叫小明我做过一个 Godot 项目"
    hyp = "大家好我叫小明我做过一个 Godot 项目"     # Mock 引擎的确定性输出
    assert cer(ref, hyp, lang="zh") < 0.08
