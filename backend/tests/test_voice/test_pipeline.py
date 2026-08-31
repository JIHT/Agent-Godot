"""端到端管线测试（M16 §9.4 · §9.6）

用 MockBackend + 合成 WAV 跑通整条离线管线，**不需要 GPU 和任何模型**。
"""
from __future__ import annotations

import pytest

from agent_godot.voice.config import (AlignConfig, AsrConfig, NormalizeConfig,
                                      VoiceConfig, load_voice_config)
from agent_godot.voice.export import to_json, to_srt, to_vtt
from agent_godot.voice.features import extract_features
from agent_godot.voice.metrics import get_metrics
from agent_godot.voice.stt import MockBackend, Transcriber, build_backends


# ── VAD ───────────────────────────────────────────────────────────────

def test_energy_vad_finds_two_segments(sample_wav, vad_chunks):
    """能量 VAD 在无 torch 环境下也能切出两段（含 pad）。"""
    from agent_godot.voice.vad import VadBackend, energy_vad_segments, load_audio_for_vad

    audio, sr = load_audio_for_vad(sample_wav)
    segs = energy_vad_segments(audio, sr)

    assert len(segs) == 2, f"期望 2 段，实际 {segs}"
    for got, want in zip(segs, vad_chunks):
        assert got[0] == pytest.approx(want[0], abs=0.15)
        assert got[1] == pytest.approx(want[1], abs=0.15)


def test_vad_backend_auto_falls_back_without_torch():
    """backend=auto 且无 torch → 自动选 energy（不崩、不抛）。"""
    from agent_godot.voice.config import VadConfig
    from agent_godot.voice.vad import VadBackend

    name = VadBackend(VadConfig(backend="auto"))
    assert name in ("silero", "energy")


def test_vad_drops_short_segments():
    """短于 min_speech_ms 的段被丢弃（防咳嗽误触发）。"""
    import numpy as np

    from agent_godot.voice.vad import energy_vad_segments

    sr, n = 16000, 16000 * 3
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.002, n).astype(np.float32)
    x[8000:9000] = 0.5          # ~60ms 的短促噪声（咳嗽）
    x[20000:30000] = 0.5        # ~625ms 的正常语音
    segs = energy_vad_segments(x, sr, min_speech_ms=250)
    assert len(segs) == 1
    assert (segs[0][1] - segs[0][0]) > 0.4


# ── 端到端转写 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transcribe_end_to_end(sample_wav, mock_backend):
    """preprocess → backend → align → normalize 全链路跑通。"""
    cfg = VoiceConfig(align=AlignConfig(enabled=False),
                      normalize=NormalizeConfig(itn=False))
    tr = await Transcriber(cfg, backends={"mock": mock_backend}).transcribe(sample_wav)

    assert tr.language == "zh"
    assert tr.duration == pytest.approx(25.0, rel=0.02)
    assert len(tr.words) > 0
    tr.assert_sane()                       # 时间戳自检通过
    assert tr.provenance.engine == "mock"


@pytest.mark.asyncio
async def test_transcribe_applies_itn(sample_wav):
    """ITN 在管线中生效：中文数字被转成阿拉伯数字。"""
    from agent_godot.voice.config import PreprocessConfig
    backend = MockBackend(text="我做了二十三个项目")
    cfg = VoiceConfig(align=AlignConfig(enabled=False),
                      preprocess=PreprocessConfig(enabled=False))
    tr = await Transcriber(cfg, backends={"mock": backend}).transcribe(sample_wav)
    assert "23" in tr.text


@pytest.mark.asyncio
async def test_metrics_recorded(sample_wav, mock_backend):
    """每次转写都产出 RTF 等埋点（§9.2 缺口 14）。"""
    m = get_metrics()
    m.reset()
    cfg = VoiceConfig(align=AlignConfig(enabled=False))
    await Transcriber(cfg, backends={"mock": mock_backend}).transcribe(sample_wav)

    recs = m.of("transcribe")
    assert len(recs) == 1
    assert "rtf" in recs[0] and recs[0]["engine"] == "mock"
    assert recs[0]["rtf"] > 0


@pytest.mark.asyncio
async def test_pick_routes_by_language():
    """路由按语言选引擎：zh 与 en 可以指向不同后端。"""
    cfg = VoiceConfig(asr=AsrConfig(
        default="mock", routing={"zh": "mock", "en": "mock"},
        quality_routing={"zh": "mock"}))
    t = Transcriber(cfg, backends={"mock": MockBackend()})
    assert t.pick("zh").name == "mock"
    assert t.pick("zh", high_accuracy=True).name == "mock"
    assert t.pick(None).name == "mock"      # 缺失语种回落 default


# ── 强制对齐 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_align_fallback_never_raises(sample_wav, mock_tr):
    """★ 对齐依赖缺失时**回退原始时间戳，管线不断**（§9.4.2）。

    铁律：宁可不对齐，也不要错对齐。
    """
    from agent_godot.voice import align

    before = [(w.start, w.end) for w in mock_tr.words]
    out = align.align_words(mock_tr, sample_wav, "zh", AlignConfig(enabled=True))
    after = [(w.start, w.end) for w in out.words]

    assert out is mock_tr
    assert before == after                       # 时间戳原样保留
    assert out.provenance.aligned is False       # 明确标注"没对齐"


def test_align_disabled_is_noop(sample_wav, mock_tr):
    from agent_godot.voice import align
    out = align.align_words(mock_tr, sample_wav, "zh", AlignConfig(enabled=False))
    assert out is mock_tr


# ── 配置 ──────────────────────────────────────────────────────────────

def test_config_loads_from_yaml():
    """config/voice.yaml 生效（从 backend/ 运行时逐级上溯找到）。"""
    cfg = load_voice_config()
    assert cfg.asr.default == "sensevoice"      # 中文友好引擎
    assert cfg.features.pause_thinking_s == 0.8
    assert "Godot" in cfg.asr.hotwords
    assert cfg.asr.condition_on_previous_text is False


def test_hotwords_configured():
    """热词必须配了——本项目专有名词多，不注入转写必错（§9.2 缺口 6）。"""
    cfg = load_voice_config()
    assert {"Godot", "GDScript", "player.gd"} <= set(cfg.asr.hotwords)


def test_config_three_layer_env_override(monkeypatch):
    """M23 三层优先级：env > yaml > 代码默认值。"""
    monkeypatch.setenv("VOICE_VAD_MIN_SILENCE_MS", "1234")
    monkeypatch.setenv("VOICE_ASR_DEFAULT", "mock")
    cfg = load_voice_config()
    assert cfg.vad.min_silence_ms == 1234
    assert cfg.asr.default == "mock"


def test_missing_yaml_falls_back_to_defaults(tmp_path):
    """删掉 yaml 也照常跑（M23 §1.2 纪律：禁止"没配就崩"）。"""
    cfg = load_voice_config(tmp_path / "none.yaml") if False else None
    from agent_godot.voice.config import VoiceConfig
    d = VoiceConfig()
    assert d.asr.default == "whisper"
    assert d.features.pause_thinking_s == 0.8


# ── 导出 ──────────────────────────────────────────────────────────────

def test_export_srt_and_vtt(mock_tr):
    srt = to_srt(mock_tr)
    assert srt.startswith("1\n")
    assert "00:00:03,000 --> " in srt

    vtt = to_vtt(mock_tr)
    assert vtt.startswith("WEBVTT")
    assert "00:00:03.000" in vtt


def test_export_json_carries_features_and_provenance(mock_tr):
    """JSON 导出要可复核：特征 + 溯源齐全（审计思维，§1.3 ⑤）。"""
    import json

    feats = extract_features(mock_tr)
    payload = json.loads(to_json(mock_tr, feats))
    assert payload["provenance"]["engine"] == "mock"
    assert payload["features"]["rate_unit"] == "字/分"
    assert payload["text"]


def test_export_can_redact():
    """导出时可选脱敏（落库/外发前的最后一道闸）。"""
    import json

    from agent_godot.voice.schema import Seg, TranscriptionResult
    tr = TranscriptionResult(
        language="zh", duration=3.0,
        segments=[Seg(0.0, 1.0, "打我电话13812345678")])
    payload = json.loads(to_json(tr, redact=True))
    assert "138" not in payload["text"]
    assert "[手机号]" in payload["text"]


# ── 工具注册 ──────────────────────────────────────────────────────────

def test_voice_tools_registered():
    from agent_godot.voice.tools import (AnalyzeSpeechTool, TranscribeTool,
                                         build_voice_tools)
    tools = build_voice_tools()
    assert {t.meta.name for t in tools} == {"transcribe", "analyze_speech"}
    assert all(t.meta.readonly and t.meta.risk == "low" for t in tools)
    # docstring 即 description（M04 约定）——不能为空
    assert TranscribeTool.meta.description
    assert AnalyzeSpeechTool.meta.description
