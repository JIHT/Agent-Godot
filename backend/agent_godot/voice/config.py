"""voice/config.py —— 语音配置面（M16 §9.5 · 遵循 M23「配置外置化」）

三层优先级（M23 §1.1）：**环境变量 > config/voice.yaml > 代码内默认值**。
- L3 代码默认值：本文件 dataclass 的字段默认值（删掉 yaml 任何一项系统照常跑）
- L2 yaml：用户日常可调项
- L1 环境变量：形如 `VOICE_ASR_DEFAULT` / `VOICE_VAD_MIN_SILENCE_MS`（CI 临时覆盖）

纪律（M23 §1.2）：**禁止"yaml 没配就崩"**——缺配即用默认值 + 一条 debug 日志。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATHS = (
    Path("config/voice.yaml"),
    Path("../config/voice.yaml"),
    Path("../../config/voice.yaml"),
)

ENV_PREFIX = "VOICE_"


# ── 配置数据结构（L3 默认值都在这里，是唯一事实源）────────────────────

@dataclass
class AsrConfig:
    default: str = "whisper"
    routing: dict[str, str] = field(default_factory=lambda: {"default": "whisper"})
    quality_routing: dict[str, str] = field(default_factory=dict)
    engines: dict[str, dict] = field(default_factory=dict)
    hotwords: list[str] = field(default_factory=list)
    condition_on_previous_text: bool = False
    hallucination_silence_threshold: float | None = 2.0
    word_timestamps: bool = True
    language: str | None = None
    min_language_prob: float = 0.5
    timeout_s: float = 600.0
    max_retries: int = 2


@dataclass
class VadConfig:
    backend: str = "auto"            # silero | energy | auto
    threshold: float = 0.5
    min_speech_ms: int = 250
    min_silence_ms: int = 500
    speech_pad_ms: int = 200


@dataclass
class PreprocessConfig:
    enabled: bool = True
    target_sr: int = 16000
    loudnorm: bool = True
    target_lufs: float = -16.0
    highpass_hz: float = 60.0


@dataclass
class AlignConfig:
    enabled: bool = True
    beam_width: int = 2
    fallback_to_asr: bool = True


@dataclass
class NormalizeConfig:
    itn: bool = True
    restore_punctuation: bool = False
    punc_gap_s: float = 0.6
    redact_pii: bool = True


@dataclass
class DiarizeConfig:
    enabled: bool = False
    backend: str = "cam++"


@dataclass
class RateRef:
    unit: str = "字/分"
    slow: float = 140.0
    normal: tuple[float, float] = (180.0, 240.0)
    fast: float = 300.0


@dataclass
class FeaturesConfig:
    pause_thinking_s: float = 0.8
    pause_stuck_s: float = 2.0
    filler_words: list[str] = field(default_factory=lambda: [
        "就是", "然后", "那个", "嗯", "呃", "这个", "啊", "的话",
        "like", "um", "uh", "you know"])
    filler_normal_rate: tuple[float, float] = (0.05, 0.08)
    quote_exempt: bool = True
    rhythm_window: int = 10
    en_word_to_zh_char: float = 1.5
    speech_rate_ref: dict[str, RateRef] = field(default_factory=lambda: {
        "zh": RateRef("字/分", 140.0, (180.0, 240.0), 300.0),
        "en": RateRef("词/分", 110.0, (130.0, 170.0), 190.0),
    })


@dataclass
class DiagnoseConfig:
    enabled: bool = True
    max_transcript_chars: int = 12000


@dataclass
class TtsConfig:
    default: str = "edge-tts"
    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: str = "+0%"
    sample_rate: int = 24000
    sentence_max_chars: int = 18
    code_symbol_pronunciation: bool = True


@dataclass
class RealtimeConfig:
    semantic_endpoint: bool = False
    endpoint_silence_ms: int = 500
    min_utterance_ms: int = 250
    barge_in_ms: int = 200
    asr_policy: str = "localagreement"
    min_chunk_s: float = 1.0
    max_context_chars: int = 4000


@dataclass
class PrivacyConfig:
    keep_audio: bool = False
    retain_days: int = 7


@dataclass
class MetricsConfig:
    enabled: bool = True


@dataclass
class VoiceConfig:
    asr: AsrConfig = field(default_factory=AsrConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    align: AlignConfig = field(default_factory=AlignConfig)
    normalize: NormalizeConfig = field(default_factory=NormalizeConfig)
    diarize: DiarizeConfig = field(default_factory=DiarizeConfig)
    features: FeaturesConfig = field(default_factory=FeaturesConfig)
    diagnose: DiagnoseConfig = field(default_factory=DiagnoseConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    realtime: RealtimeConfig = field(default_factory=RealtimeConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    source: Path | None = None


# ── 加载 ──────────────────────────────────────────────────────────────

def _rate_ref(raw: dict) -> RateRef:
    normal = raw.get("normal") or [180.0, 240.0]
    return RateRef(
        unit=raw.get("unit", "字/分"),
        slow=float(raw.get("slow", 140.0)),
        normal=(float(normal[0]), float(normal[1])),
        fast=float(raw.get("fast", 300.0)),
    )


def _build(data: dict, source: Path | None) -> VoiceConfig:
    """从 yaml dict 构建；缺失的节/键一律回落到 dataclass 默认值。"""
    a = data.get("asr") or {}
    v = data.get("vad") or {}
    p = data.get("preprocess") or {}
    al = data.get("align") or {}
    n = data.get("normalize") or {}
    d = data.get("diarize") or {}
    f = data.get("features") or {}
    dg = data.get("diagnose") or {}
    t = data.get("tts") or {}
    r = data.get("realtime") or {}
    pr = data.get("privacy") or {}
    m = data.get("metrics") or {}

    refs = {k: _rate_ref(vv) for k, vv in (f.get("speech_rate_ref") or {}).items()}
    feats = FeaturesConfig(
        pause_thinking_s=float(f.get("pause_thinking_s", 0.8)),
        pause_stuck_s=float(f.get("pause_stuck_s", 2.0)),
        filler_words=list(f.get("filler_words") or FeaturesConfig().filler_words),
        filler_normal_rate=tuple(f.get("filler_normal_rate") or (0.05, 0.08)),  # type: ignore[arg-type]
        quote_exempt=bool(f.get("quote_exempt", True)),
        rhythm_window=int(f.get("rhythm_window", 10)),
        en_word_to_zh_char=float(f.get("en_word_to_zh_char", 1.5)),
        speech_rate_ref=refs or FeaturesConfig().speech_rate_ref,
    )

    cfg = VoiceConfig(
        asr=AsrConfig(
            default=a.get("default", "whisper"),
            routing=dict(a.get("routing") or {"default": "whisper"}),
            quality_routing=dict(a.get("quality_routing") or {}),
            engines={k: dict(vv) for k, vv in (a.get("engines") or {}).items()},
            hotwords=list(a.get("hotwords") or []),
            condition_on_previous_text=bool(a.get("condition_on_previous_text", False)),
            hallucination_silence_threshold=a.get("hallucination_silence_threshold", 2.0),
            word_timestamps=bool(a.get("word_timestamps", True)),
            language=a.get("language"),
            min_language_prob=float(a.get("min_language_prob", 0.5)),
            timeout_s=float(a.get("timeout_s", 600.0)),
            max_retries=int(a.get("max_retries", 2)),
        ),
        vad=VadConfig(
            backend=v.get("backend", "auto"),
            threshold=float(v.get("threshold", 0.5)),
            min_speech_ms=int(v.get("min_speech_ms", 250)),
            min_silence_ms=int(v.get("min_silence_ms", 500)),
            speech_pad_ms=int(v.get("speech_pad_ms", 200)),
        ),
        preprocess=PreprocessConfig(
            enabled=bool(p.get("enabled", True)),
            target_sr=int(p.get("target_sr", 16000)),
            loudnorm=bool(p.get("loudnorm", True)),
            target_lufs=float(p.get("target_lufs", -16.0)),
            highpass_hz=float(p.get("highpass_hz", 60.0)),
        ),
        align=AlignConfig(
            enabled=bool(al.get("enabled", True)),
            beam_width=int(al.get("beam_width", 2)),
            fallback_to_asr=bool(al.get("fallback_to_asr", True)),
        ),
        normalize=NormalizeConfig(
            itn=bool(n.get("itn", True)),
            restore_punctuation=bool(n.get("restore_punctuation", False)),
            punc_gap_s=float(n.get("punc_gap_s", 0.6)),
            redact_pii=bool(n.get("redact_pii", True)),
        ),
        diarize=DiarizeConfig(
            enabled=bool(d.get("enabled", False)),
            backend=d.get("backend", "cam++"),
        ),
        features=feats,
        diagnose=DiagnoseConfig(
            enabled=bool(dg.get("enabled", True)),
            max_transcript_chars=int(dg.get("max_transcript_chars", 12000)),
        ),
        tts=TtsConfig(
            default=t.get("default", "edge-tts"),
            voice=t.get("voice", "zh-CN-XiaoxiaoNeural"),
            rate=t.get("rate", "+0%"),
            sample_rate=int(t.get("sample_rate", 24000)),
            sentence_max_chars=int(t.get("sentence_max_chars", 18)),
            code_symbol_pronunciation=bool(t.get("code_symbol_pronunciation", True)),
        ),
        realtime=RealtimeConfig(
            semantic_endpoint=bool(r.get("semantic_endpoint", False)),
            endpoint_silence_ms=int(r.get("endpoint_silence_ms", 500)),
            min_utterance_ms=int(r.get("min_utterance_ms", 250)),
            barge_in_ms=int(r.get("barge_in_ms", 200)),
            asr_policy=r.get("asr_policy", "localagreement"),
            min_chunk_s=float(r.get("min_chunk_s", 1.0)),
            max_context_chars=int(r.get("max_context_chars", 4000)),
        ),
        privacy=PrivacyConfig(
            keep_audio=bool(pr.get("keep_audio", False)),
            retain_days=int(pr.get("retain_days", 7)),
        ),
        metrics=MetricsConfig(enabled=bool(m.get("enabled", True))),
        source=source,
    )
    _apply_env(cfg)
    return cfg


# ── L1 环境变量覆盖 ───────────────────────────────────────────────────

_ENV_MAP: dict[str, tuple[str, str, str]] = {
    # 环境变量名 → (节, 字段, 类型)
    "ASR_DEFAULT": ("asr", "default", "s"),
    "ASR_LANGUAGE": ("asr", "language", "s"),
    "ASR_TIMEOUT_S": ("asr", "timeout_s", "f"),
    "ASR_MAX_RETRIES": ("asr", "max_retries", "i"),
    "VAD_BACKEND": ("vad", "backend", "s"),
    "VAD_MIN_SILENCE_MS": ("vad", "min_silence_ms", "i"),
    "VAD_SPEECH_PAD_MS": ("vad", "speech_pad_ms", "i"),
    "ALIGN_ENABLED": ("align", "enabled", "b"),
    "NORMALIZE_ITN": ("normalize", "itn", "b"),
    "NORMALIZE_REDACT_PII": ("normalize", "redact_pii", "b"),
    "DIARIZE_ENABLED": ("diarize", "enabled", "b"),
    "TTS_DEFAULT": ("tts", "default", "s"),
    "TTS_VOICE": ("tts", "voice", "s"),
    "REALTIME_ASR_POLICY": ("realtime", "asr_policy", "s"),
    "REALTIME_SEMANTIC_ENDPOINT": ("realtime", "semantic_endpoint", "b"),
    "PRIVACY_KEEP_AUDIO": ("privacy", "keep_audio", "b"),
    "METRICS_ENABLED": ("metrics", "enabled", "b"),
}


def _apply_env(cfg: VoiceConfig) -> None:
    """L1 覆盖：VOICE_<KEY> 存在则按类型强制转换后写入对应字段。"""
    for key, (section, attr, kind) in _ENV_MAP.items():
        raw = os.environ.get(ENV_PREFIX + key)
        if raw is None:
            continue
        try:
            if kind == "i":
                val: object = int(raw)
            elif kind == "f":
                val = float(raw)
            elif kind == "b":
                val = raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                val = None if raw.strip().lower() in ("", "null", "none") else raw
        except ValueError:
            logger.warning("环境变量 %s%s 值非法: %r，忽略", ENV_PREFIX, key, raw)
            continue
        setattr(getattr(cfg, section), attr, val)
        logger.debug("配置覆盖 %s%s → %s.%s = %r", ENV_PREFIX, key, section, attr, val)


def load_voice_config(path: str | Path | None = None) -> VoiceConfig:
    """工厂：显式路径 → 逐级上溯找 config/voice.yaml → 全默认值。

    找不到配置文件时**不抛异常**，回落到纯默认值（M23 §1.2 纪律）。
    """
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        return _load_yaml(p)
    for p in DEFAULT_CONFIG_PATHS:
        if p.exists():
            return _load_yaml(p)
    logger.info("未找到 config/voice.yaml，使用代码内默认配置")
    return VoiceConfig()


def _load_yaml(path: Path) -> VoiceConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        logger.warning("config/voice.yaml 解析失败，回落默认值: %s", e)
        return VoiceConfig()
    return _build(data, path)
