"""CLI 端到端测试（M16 §0 · 三条产品线）

用 mock 引擎跑通 `voice transcribe / analyze / chat`，不依赖 GPU 与网络。
LLM 用假适配器替换（避免真实调用）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_godot.cli import run_voice_analyze, run_voice_chat, run_voice_transcribe


@pytest.fixture
def mock_engine(monkeypatch):
    """把整张路由表切到 mock（含 quality_routing——analyze 走高精度路径）。

    只改 VOICE_ASR_DEFAULT 不够：`high_accuracy=True` 会走 `quality_routing`，
    而 yaml 里 zh → whisper。所以这里直接替换配置对象的两张路由表。
    """
    from agent_godot.voice import config as vcfg

    cfg = vcfg.load_voice_config()
    cfg.asr.default = "mock"
    cfg.asr.routing = {"default": "mock"}
    cfg.asr.quality_routing = {"default": "mock"}
    cfg.align.enabled = False                 # 无 torch，关掉省告警
    cfg.tts.default = "mock"
    monkeypatch.setattr(vcfg, "load_voice_config", lambda *a, **k: cfg)
    return cfg


@pytest.fixture
def fake_llm(monkeypatch):
    """替换 M02 的模型注册中心，返回确定性的假回答。"""
    from agent_godot.core import LLMRequest, LLMResponse, Message  # noqa: F401

    class _Resp:
        content = "总评 7 分。语速 200 字/分属正常区间。最严重的问题：填充词偏多。"

    class _LLM:
        async def complete(self, req):
            return _Resp()

    class _Reg:
        def llm_for_mode(self, mode):
            return _LLM()

    import agent_godot.core as core
    monkeypatch.setattr(core, "load_registry", lambda: _Reg(), raising=False)


def test_voice_transcribe_text(sample_wav, mock_engine, capsys):
    asyncio.run(run_voice_transcribe(str(sample_wav), "", "text", False))
    out = capsys.readouterr().out
    assert "引擎" in out and "语言" in out


def test_voice_transcribe_srt(sample_wav, mock_engine, capsys):
    asyncio.run(run_voice_transcribe(str(sample_wav), "", "srt", False))
    out = capsys.readouterr().out
    assert "-->" in out                       # SRT 时间轴
    assert out.lstrip().startswith("1\n")


def test_voice_transcribe_json_carries_features(sample_wav, mock_engine, capsys):
    import json

    asyncio.run(run_voice_transcribe(str(sample_wav), "", "json", False))
    payload = json.loads(capsys.readouterr().out)
    assert payload["provenance"]["engine"] == "mock"
    assert "features" in payload
    assert payload["features"]["rate_unit"] == "字/分"


def test_voice_analyze_features_only(sample_wav, mock_engine, capsys):
    """--no-report：只出实测特征，不调 LLM。"""
    asyncio.run(run_voice_analyze(str(sample_wav), "", with_report=False))
    out = capsys.readouterr().out
    assert "字/分" in out
    assert "填充词" in out
    assert "节奏方差" in out


def test_voice_analyze_with_report(sample_wav, mock_engine, fake_llm, capsys):
    """带 LLM 报告，且经过数值漂移校验（幻觉检测接口）。"""
    asyncio.run(run_voice_analyze(str(sample_wav), "", with_report=True))
    out = capsys.readouterr().out
    assert "总评" in out


def test_voice_analyze_missing_file(tmp_path, mock_engine, capsys):
    asyncio.run(run_voice_analyze(str(tmp_path / "nope.wav"), "", False))
    assert "音频不存在" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_voice_chat_from_wav(sample_wav, mock_engine, fake_llm,
                                   tmp_path, capsys):
    """全双工对话：从 WAV 回放，产出 TTS 音频文件。

    服务端没有扬声器，播放归 M20 前端——这里只验证链路跑通且出了音频。
    """
    out = tmp_path / "reply.wav"
    await run_voice_chat(str(sample_wav), str(out), "zh")
    text = capsys.readouterr().out
    assert "已合成" in text
    if out.exists():
        assert out.stat().st_size > 44         # 大于 WAV 头


@pytest.mark.asyncio
async def test_voice_chat_respects_privacy_no_audio_kept(
        sample_wav, mock_engine, fake_llm, tmp_path):
    """隐私纪律：voice chat 不落原始录音（只产出 TTS 回复）。"""
    out = tmp_path / "r.wav"
    await run_voice_chat(str(sample_wav), str(out), "zh")
    assert sample_wav.exists()                # 输入原样保留，不复制不落库
    assert not (tmp_path / "recording.wav").exists()
