"""时间戳正确性测试（M16 §1.1 ⑤a · §9.6）

这一组是**最重要**的回归防线：VAD 把静音剪掉再拼接后，时间戳若没还原，
所有的时间类指标（停顿/语速/节奏）全部失真，而且**失真得很隐蔽**——
数字还在，只是全错。
"""
from __future__ import annotations

import pytest

from agent_godot.voice.features import pauses
from agent_godot.voice.schema import (Provenance, Seg, TranscriptionResult,
                                      WordInfo)
from agent_godot.voice.vad import TimelineMapper, restore_time


def test_vad_offset_restores_pauses(vad_chunks):
    """6.2s 卡壳在拼接轴上只有 0.6s。

    未还原 → 0.6s < 0.8s 阈值 → **停顿消失**（判成"完全流利"）
    还原后 → 6.2s > 2.0s → 判"卡壳" ✓
    """
    mapper = TimelineMapper(vad_chunks)

    # 拼接轴：第一段长 9.4s，第二段从 9.4 开始
    compressed = [(0.2, 9.0), (9.6, 16.0)]
    restored = mapper.restore(compressed)

    # 原始轴：第一段 3.0~11.8，第二段 18.0~24.4
    assert restored[0][0] == pytest.approx(3.0, abs=0.01)
    assert restored[0][1] == pytest.approx(11.8, abs=0.01)
    assert restored[1][0] == pytest.approx(18.0, abs=0.01)

    # 真实间隙 6.2s
    gap = restored[1][0] - restored[0][1]
    assert gap == pytest.approx(6.2, abs=0.01)

    # 未还原的间隙只有 0.6s（被压成 2×pad 量级）
    naive_gap = compressed[1][0] - compressed[0][1]
    assert naive_gap == pytest.approx(0.6, abs=0.01)


def test_compressed_axis_kills_all_pauses():
    """★ 核心结论：拼接轴上所有停顿被抹平成常数（2×pad），全部掉到阈值以下。

    真实语音四段，真实停顿分别是 1.0s（思考）、3.0s、7.0s（卡壳）。
    在拼接轴上三者全都变成 0.4s，一个停顿都检不出来。
    """
    # 真实语音区：(10,12) (13,20) (23,27) (34,38)；pad=0.2
    chunks = [(9.8, 12.2), (12.8, 20.2), (22.8, 27.2), (33.8, 38.2)]
    mapper = TimelineMapper(chunks)

    lens = [e - s for s, e in chunks]
    compressed, acc = [], 0.0
    for L in lens:                                  # 每段一个"词"，两端各留 pad
        compressed.append((round(acc + 0.2, 3), round(acc + L - 0.2, 3)))
        acc += L

    # 拼接轴上的三个间隙全是 0.4（= 2×pad），与真实停顿多长无关
    for a, b in zip(compressed, compressed[1:]):
        assert round(b[0] - a[1], 6) == pytest.approx(0.4)

    def _mk(pairs):
        return [WordInfo(text=f"w{i}", start=a, end=b)
                for i, (a, b) in enumerate(pairs)]

    # 未还原：一个停顿都检不出（0.8s 阈值）→ 停顿指标整体归零
    assert pauses(_mk(compressed), 0.8, 2.0) == []

    # 还原后：三个停顿全在，且**时长与分类**都正确
    ps = pauses(_mk(mapper.restore(compressed)), 0.8, 2.0)
    assert len(ps) == 3
    assert [p.kind for p in ps] == ["思考", "卡壳", "卡壳"]
    assert [p.duration for p in ps] == pytest.approx([1.0, 3.0, 7.0], abs=0.01)


def test_no_double_pad(vad_chunks):
    """pad 只出现一次：映射基准就是含 pad 的 chunk.start。

    常见错误：还原时又减了一次 pad → 时间戳整体前移 0.2s。
    """
    # 拼接轴原点应映射到第一段起点 2.8（= 语音起点 3.0 − pad 0.2）
    assert restore_time(0.0, vad_chunks) == pytest.approx(2.8, abs=1e-6)
    # 不是 3.0（多减了 pad），也不是 2.6（多加了）
    assert restore_time(0.0, vad_chunks) != pytest.approx(3.0, abs=0.05)

    # 第二段在拼接轴的起点 9.4 → 原始轴 17.8
    assert restore_time(9.4, vad_chunks) == pytest.approx(17.8, abs=1e-6)


def test_axis_warning_detects_compression():
    """axis_warning 能"时间戳还停留在压缩轴"这个错误。

    判据：拼接轴总长 == duration_after_vad。末词时间戳若 ≤ duration_after_vad
    而 VAD 又剪掉了可观比例的音频 → 高度可疑。

    注意它返回**告警**而不是抛异常——录音末尾本来就可能有一段真空静音，
    那时 last.end ≈ duration_after_vad 是正常的（见下一条测试）。
    """
    from agent_godot.voice.schema import TranscriptionResult

    def mk(second_start: float):
        ws = [WordInfo(text=c, start=0.2 + i * 0.9, end=0.38 + i * 0.9)
              for i, c in enumerate("大家好我叫小明")]
        ws += [WordInfo(text=c, start=second_start + i * 0.9,
                        end=second_start + 0.18 + i * 0.9)
               for i, c in enumerate("我做过一个项目")]
        return TranscriptionResult(
            language="zh", duration=25.0, duration_after_vad=16.4,
            segments=[Seg(0.0, 0.0, "", words=ws)])

    # 压缩轴：末词 15.18s ≤ duration_after_vad 16.4s，且 VAD 剪掉 34% → 告警
    assert "拼接轴" in (mk(9.6).axis_warning() or "")

    # 原始轴：末词 23.58s 远超 16.4s → 干净
    assert mk(18.0).axis_warning() is None


def test_axis_warning_silent_when_vad_barely_trims():
    """VAD 几乎没剪音频时两轴本就接近 → 不该误报。"""
    from agent_godot.voice.schema import TranscriptionResult

    tr = TranscriptionResult(
        language="zh", duration=10.0, duration_after_vad=9.9,
        segments=[Seg(0.0, 0.0, "",
                      words=[WordInfo("你", 0.0, 0.3), WordInfo("好", 0.4, 0.7)])])
    assert tr.axis_warning() is None


def test_timestamps_sane_accepts_restored(mock_tr):
    """还原后的时间戳通过自检（mock_tr 是原始轴坐标）。"""
    mock_tr.assert_sane()          # 不抛即通过


def test_non_monotonic_and_overflow_detected():
    """非单调、负时间戳、超出总长——三种畸形都能被抓住。"""
    from agent_godot.voice.schema import TranscriptionResult

    def mk(words, duration=10.0):
        return TranscriptionResult(
            language="zh", duration=duration,
            segments=[Seg(0.0, 0.0, "", words=words)])

    overlap = [WordInfo("a", 0.0, 1.0), WordInfo("b", 0.5, 1.5)]
    with pytest.raises(AssertionError, match="非单调"):
        mk(overlap).assert_sane()

    negative = [WordInfo("a", -0.5, 0.2), WordInfo("b", 1.0, 1.5)]
    with pytest.raises(AssertionError, match="负时间戳"):
        mk(negative).assert_sane()

    overflow = [WordInfo("a", 0.0, 1.0), WordInfo("b", 20.0, 21.0)]
    with pytest.raises(AssertionError, match="超出音频总长"):
        mk(overflow).assert_sane()


def test_duration_after_vad_trim_rate(vad_chunks):
    """VAD 裁剪率 = 1 - duration_after_vad / duration（可观测指标之一）。"""
    from agent_godot.voice.schema import TranscriptionResult

    tr = TranscriptionResult(language="zh", duration=25.0, duration_after_vad=16.4)
    assert tr.vad_trim_rate == pytest.approx(1 - 16.4 / 25.0, abs=1e-6)