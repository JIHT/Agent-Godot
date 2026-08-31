"""voice：STT 与语音分析（M16）—— 给 Agent 装耳朵和嘴巴。

三条产品线（§0）：
① **语音输入**——对麦克风说"给玩家加双跳"，转文字进 Query Engine 走原管线
② **口语诊断**——分析录音的语速/停顿/填充词，产出带实测数值的报告
③ **实时语音对话**——全双工：边听边答、可随时打断

核心管线：**preprocess → VAD+ASR → 强制对齐 → ITN → 特征 → LLM 诊断**。
实时链路：**端点检测（物理+语义）→ ASR 重叠预热 → LLM 流式分句 → TTS 流式 → 打断**。

★ 设计铁律（§9.4.1）：**声学模型是可替换件**。
  换引擎只动 `config/voice.yaml` 的 `asr.routing`，`TranscriptionResult`
  契约与上层（features / diagnose / tools / UI）零改动。

★ 依赖纪律：本包**顶层导入零重依赖**（只用标准库 + numpy）。
  torch / faster_whisper / funasr / edge_tts / onnxruntime 全部函数内懒导入，
  没装 GPU 环境的机器上照样能 import、能跑测试、能走 Mock 后端。
"""
from __future__ import annotations

from .config import VoiceConfig, load_voice_config
from .schema import (AsrDelta, Provenance, Seg, TranscriptionResult, TtsChunk,
                     WordInfo)
from .stt import (ASRBackend, BackendUnavailable, MockBackend, RemoteASRBackend,
                  Transcriber, WhisperBackend, build_backends)

__all__ = [
    # 契约
    "TranscriptionResult", "Seg", "WordInfo", "Provenance",
    "AsrDelta", "TtsChunk",
    # 配置
    "VoiceConfig", "load_voice_config",
    # ASR
    "Transcriber", "ASRBackend", "WhisperBackend", "RemoteASRBackend",
    "MockBackend", "build_backends", "BackendUnavailable",
]

__version__ = "0.1.0"


def __getattr__(name: str):
    """懒导出：子模块按需导入，避免顶层拉起重依赖。"""
    _MODULES = {
        "vad": "vad", "preprocess": "preprocess", "align": "align",
        "normalize": "normalize", "diarize": "diarize", "features": "features",
        "diagnose": "diagnose", "stream_asr": "stream_asr", "turn": "turn",
        "tts": "tts", "realtime": "realtime", "export": "export",
        "metrics": "metrics", "tools": "tools",
    }
    if name in _MODULES:
        import importlib
        mod = importlib.import_module(f".{_MODULES[name]}", __name__)
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
