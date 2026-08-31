"""voice/diarize.py —— 说话人分离（M16 §9.2 缺口 3）

会议/多人场景才需要，单人口语诊断不必付这份算力 → 配置默认关闭。

★ 隐私红线（§9.2 明确不采纳项）：**只做 diarization（"谁在什么时候说话"的
  编号），绝不做 speaker identification（声纹→身份）**。声纹是生物特征，
  与 §1.3"隐私从紧"原则冲突。输出标签只有 `SPEAKER_00/01`，不含真实姓名。

重依赖（pyannote.audio / funasr）全部函数内懒导入；不可用时返回空结果，
调用方（`assign_speakers`）原样返回转写结果——分离是增强，不是必需。
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import DiarizeConfig
from .schema import TranscriptionResult

logger = logging.getLogger(__name__)

__all__ = ["diarize", "assign_speakers", "DiarizeUnavailable", "SpeakerTurn"]


class DiarizeUnavailable(RuntimeError):
    """分离依赖缺失或服务不可达。"""


SpeakerTurn = tuple[float, float, str]      # (start_s, end_s, speaker)


def diarize(wav: Path, cfg: DiarizeConfig | None = None) -> list[SpeakerTurn]:
    """返回说话人轮次 [(start, end, "SPEAKER_00"), ...]。失败返回空列表。"""
    from .config import DiarizeConfig as _DC
    cfg = cfg or _DC()
    try:
        if cfg.backend in ("pyannote", "pyannote-3.1"):
            return _pyannote(wav)
        if cfg.backend in ("cam++", "camplusplus", "funasr"):
            return _campp(wav)
        raise DiarizeUnavailable(f"未知分离后端: {cfg.backend}")
    except DiarizeUnavailable:
        raise
    except Exception as e:                             # noqa: BLE001
        logger.warning("说话人分离失败，跳过（不影响主流程）: %s", e)
        return []


def _pyannote(wav: Path) -> list[SpeakerTurn]:
    try:
        from pyannote.audio import Pipeline
    except ImportError as e:
        raise DiarizeUnavailable(
            "未安装 pyannote.audio。安装：uv pip install pyannote.audio "
            "（还需在 hf.co 接受 pyannote/speaker-diarization-3.1 的使用条款）"
        ) from e
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1", use_auth_token=True)
    ann = pipeline(str(wav))
    return [(float(t.start), float(t.end), f"SPEAKER_{_idx(label)}")
            for t, _, label in ann.itertracks(yield_label=True)]


def _idx(label: str) -> int:
    """把 pyannote 的任意标签稳定映射成序号（只留编号，不带身份）。"""
    import hashlib

    return int(hashlib.md5(label.encode()).hexdigest()[:4], 16) % 100


def _campp(wav: Path) -> list[SpeakerTurn]:
    """FunASR cam++：中文场景与 ASR 同工具包，零额外部署。"""
    try:
        from funasr import AutoModel
    except ImportError as e:
        raise DiarizeUnavailable(
            "未安装 funasr。安装：uv pip install funasr") from e
    model = AutoModel(model="cam++", model_revision="v2.0.2")
    res = model.generate(input=str(wav))
    turns: list[SpeakerTurn] = []
    for seg in (res[0].get("value") if isinstance(res, list) and res else None) or []:
        s, e = seg[0] / 1000.0, seg[1] / 1000.0
        turns.append((float(s), float(e), f"SPEAKER_{int(seg[2]):02d}"))
    return turns


# ── 标注 ──────────────────────────────────────────────────────────────

def assign_speakers(tr: TranscriptionResult, wav: Path,
                    cfg: DiarizeConfig | None = None) -> TranscriptionResult:
    """按最大重叠把说话人标签打到段与词上。分离不可用时原样返回。"""
    turns = diarize(wav, cfg)
    if not turns:
        return tr

    for seg in tr.segments:
        spk = _best_speaker(seg.start, seg.end, turns)
        if spk is None:
            continue
        seg.speaker = spk
        for w in seg.words:
            w.speaker = _best_speaker(w.start, w.end, turns) or spk
    return tr


def _best_speaker(start: float, end: float, turns: list[SpeakerTurn]) -> str | None:
    best, best_ov = None, 0.0
    for s, e, spk in turns:
        ov = min(end, e) - max(start, s)
        if ov > best_ov:
            best, best_ov = spk, ov
    return best
