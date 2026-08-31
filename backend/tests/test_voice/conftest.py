"""tests/test_voice/conftest.py —— 语音测试公共夹具（M16 §5）

零 GPU、零模型：所有音频都是**合成 WAV**（标准库 wave 写出，不需要 ffmpeg），
所有 ASR 结果都来自 `MockBackend`。这样单测在 CI 上也能跑。

合成音频的结构对应 §1.1 ⑤a 的例子：
    0~3s   静音 | 3~12s 说话 | 12~18s 静音（6s 卡壳）| 18~25s 说话
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

SR = 16000
TOTAL = 25.0
# 真实语音区（原始轴，秒）
SPEECH_REGIONS = [(3.0, 12.0), (18.0, 25.0)]
# 期望的 VAD 段（含两侧 200ms pad）
# 注意：最后一段的**右 pad 被音频总长截断**（25.2 → 25.0）——这是
# `_postprocess` 里 `min(dur, e + pad_s)` 的正确行为，不是 bug。
EXPECTED_CHUNKS = [(2.8, 12.2), (17.8, 25.0)]


def _write_wav(path: Path, data: np.ndarray, sr: int = SR) -> Path:
    pcm = np.clip(data, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


@pytest.fixture(scope="session")
def sample_wav(tmp_path_factory) -> Path:
    """25 秒合成音频：静音 3s / 说话 9s / 静音 6s / 说话 7s。"""
    rng = np.random.default_rng(42)
    n = int(TOTAL * SR)
    x = rng.normal(0.0, 0.003, n).astype(np.float32)      # 底噪
    t = np.arange(n) / SR
    for s, e in SPEECH_REGIONS:
        a, b = int(s * SR), int(e * SR)
        # 幅度调制，模拟音节起伏（能量 VAD 能稳定检出）
        env = 0.25 * (0.6 + 0.4 * np.sin(2 * np.pi * 3.0 * t[a:b]))
        x[a:b] = (env * rng.normal(0.0, 1.0, b - a)).astype(np.float32)
    return _write_wav(tmp_path_factory.mktemp("voice") / "sample.wav", x)


@pytest.fixture
def vad_chunks() -> list[tuple[float, float]]:
    """能量 VAD 在 sample_wav 上应切出的段（含 pad）。"""
    return list(EXPECTED_CHUNKS)


@pytest.fixture
def mock_tr():
    """对应 §1.1 ⑤a 例子的转写结果（**已还原到原始轴**）。

    词时间戳：第一段 3.0→11.8，第二段 18.0→24.4；中间 6.2s 卡壳。
    """
    from agent_godot.voice.schema import (Provenance, Seg, TranscriptionResult,
                                          WordInfo)

    def mk(text: str, start: float, step: float = 0.9) -> list[WordInfo]:
        return [WordInfo(text=c, start=round(start + i * step, 3),
                         end=round(start + i * step + 0.18, 3), prob=0.95)
                for i, c in enumerate(text)]

    seg1 = Seg(0.0, 0.0, "大家好我叫小明", words=mk("大家好我叫小明", 3.0))
    seg2 = Seg(0.0, 0.0, "我做过一个项目", words=mk("我做过一个项目", 18.0))
    for s in (seg1, seg2):
        s.start, s.end = s.words[0].start, s.words[-1].end
    return TranscriptionResult(
        language="zh", duration=TOTAL, duration_after_vad=16.4,
        segments=[seg1, seg2],
        provenance=Provenance(engine="mock", language="zh", language_prob=0.99))


@pytest.fixture
def mock_backend():
    from agent_godot.voice.stt import MockBackend
    return MockBackend(text="大家好我叫小明我做过一个 Godot 项目")
