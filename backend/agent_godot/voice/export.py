"""voice/export.py —— 字幕与结构化导出（M16 §9.2 缺口 14）

性价比最高的一项：几十行代码，直接让转写结果**可交付**。
Godot 侧录制旁白/台词质检的场景刚需（拖进剪辑软件、喂字幕轨）。

三种格式：
- **SRT**：通用字幕（序号 / 时间轴 / 文本）
- **VTT**：Web 字幕（WebVTT，M20 前端 <track> 可直接用）
- **JSON**：转写 + 特征 + 溯源全量（可复核、可二次分析——审计思维，§1.3 ⑤）
"""
from __future__ import annotations

import json
from typing import Any

from .features import SpeechFeatures
from .schema import TranscriptionResult

__all__ = ["to_srt", "to_vtt", "to_json", "srt_time", "vtt_time"]


def srt_time(t: float) -> str:
    """秒 → SRT 时间轴 `HH:MM:SS,mmm`。"""
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((t - int(t)) * 1000)) % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def vtt_time(t: float) -> str:
    """秒 → WebVTT 时间轴 `HH:MM:SS.mmm`。"""
    return srt_time(t).replace(",", ".")


def _chunks(tr: TranscriptionResult, max_chars: int) -> list[tuple[float, float, str]]:
    """按段（含说话人标签）生成字幕块；段过长时按词再切。"""
    out: list[tuple[float, float, str]] = []
    for seg in tr.segments:
        prefix = f"[{seg.speaker}] " if seg.speaker else ""
        text = prefix + seg.text.strip()
        if not text.strip():
            continue
        if len(text) <= max_chars or not seg.words:
            out.append((seg.start, seg.end, text))
            continue
        # 长段按词切成不超过 max_chars 的块
        cur_s = seg.words[0].start
        buf = prefix
        for w in seg.words:
            if len(buf) + len(w.text) > max_chars and buf.strip():
                out.append((cur_s, w.start, buf.strip()))
                cur_s, buf = w.start, ""
            buf += w.text
        if buf.strip():
            out.append((cur_s, seg.words[-1].end, buf.strip()))
    return [(max(0.0, s), max(s + 0.1, e), t) for s, e, t in out]


def to_srt(tr: TranscriptionResult, max_chars: int = 24) -> str:
    lines: list[str] = []
    for i, (s, e, text) in enumerate(_chunks(tr, max_chars), 1):
        lines += [str(i), f"{srt_time(s)} --> {srt_time(e)}", text, ""]
    return "\n".join(lines)


def to_vtt(tr: TranscriptionResult, max_chars: int = 24) -> str:
    lines = ["WEBVTT", ""]
    for s, e, text in _chunks(tr, max_chars):
        lines += [f"{vtt_time(s)} --> {vtt_time(e)}", text, ""]
    return "\n".join(lines)


def to_json(tr: TranscriptionResult, features: SpeechFeatures | None = None,
            *, redact: bool = False) -> str:
    """全量导出：转写 + 特征 + 溯源（可复核可二次分析）。"""
    from .normalize import redact_result

    src = redact_result(tr) if redact else tr
    payload: dict[str, Any] = {
        "transcript": src.to_dict(),
        "text": src.text,
        "provenance": {
            "engine": src.provenance.engine,
            "engine_version": src.provenance.engine_version,
            "language": src.provenance.language,
            "language_prob": src.provenance.language_prob,
            "aligned": src.provenance.aligned,
            "normalized": src.provenance.normalized,
        },
    }
    if features is not None:
        payload["features"] = {
            "speech_rate": features.speech_rate,
            "rate_unit": features.rate_unit,
            "rate_verdict": features.rate_verdict,
            "pause_count": len(features.pauses),
            "longest_pause_s": (features.longest_pause.duration
                                if features.longest_pause else 0.0),
            "fillers": features.fillers,
            "filler_density": features.filler_density,
            "rhythm_variance": features.rhythm_variance,
            "effective_speech_s": features.effective_speech_s,
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)
