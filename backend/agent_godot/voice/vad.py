"""voice/vad.py —— 静音切割（M16 §1.1 · §9.2）

VAD = 分诊护士：把"确实在说话"的段挑出来，段前后各补 pad 防切字。
三收益（§7 拷打第 1 题）：省算力（静音不跑模型）、提质量（消灭静音幻觉的
触发条件）、控段长（适配 30s 窗口）。

两个实现：
- `silero_vad_segments`：Silero-VAD（有 torch 时），逐帧语音概率，精度最好
- `energy_vad_segments`：**纯 numpy 自适应能量法**，零重依赖，无 torch 也能跑

`vad_segments()` 按配置自动分派（backend=auto 时优先 silero，缺失回落能量法）。

★ 返回的 (start, end) 是**原始音频轴**的秒数，且已含两侧 pad。
  pad 已经加进边界里了——上层映射偏移时用 `chunk_start` 就够，
  **不要再减一次 pad**（§1.1 ⑤a："不是加 pad，是多减了才叫错"）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .config import VadConfig

logger = logging.getLogger(__name__)

__all__ = ["vad_segments", "silero_vad_segments", "energy_vad_segments",
           "load_audio_for_vad", "VadBackend", "TimelineMapper", "restore_time"]


# ── 拼接轴 → 原始轴（自建 VAD 切片路径必备）─────────────────────────────

class TimelineMapper:
    """把"VAD 拼接轴"上的时间还原到"原始音频轴"（§1.1 ⑤a）。

    场景：自己 VAD 切片 → 把切片**首尾相接**喂给 whisper → whisper 吐出的
    时间戳是拼接轴坐标 → 必须映射回去，否则：

        ★ VAD 拼接后，相邻段之间的间隙恒等于 2×speech_pad_ms，
          与真实停顿多长完全无关。6.2s 的卡壳会被压成 0.6s，
          直接掉到 0.8s 阈值以下 → 停顿指标整体归零。

    pad 语义：chunk 边界**已经包含** pad（chunk.start = 语音起点 − pad），
    所以映射基准直接用 chunk.start，**不要再减一次 pad**。
    """

    def __init__(self, chunks: list[tuple[float, float]]) -> None:
        self.chunks = sorted(chunks)
        self._offsets: list[float] = []
        acc = 0.0
        for s, e in self.chunks:
            self._offsets.append(acc)
            acc += max(0.0, e - s)
        self.total = acc

    def to_original(self, t: float) -> float:
        """拼接轴 t → 原始轴秒数；超出末尾则按最后一段延展。"""
        if not self.chunks:
            return t
        for (s, e), off in zip(self.chunks, self._offsets):
            if t <= off + (e - s):
                return s + (t - off)
        last_s, last_e = self.chunks[-1]
        return last_e + (t - self.total)

    def restore(self, segs: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(self.to_original(a), self.to_original(b)) for a, b in segs]

    @property
    def compressed_gap(self) -> float:
        """拼接轴上相邻块之间的"假间隙"——这就是被抹平的停顿长度。

        两段之间在拼接轴上紧邻（间隙 0），真实间隙是 `chunks[i+1].start -
        chunks[i].end`。若代码直接用拼接轴算停顿，得到的就是这个 0。
        """
        return 0.0


def restore_time(t: float, chunks: list[tuple[float, float]]) -> float:
    """便捷函数：一次性的拼接轴 → 原始轴换算。"""
    return TimelineMapper(chunks).to_original(t)


# ── 分派 ──────────────────────────────────────────────────────────────

def VadBackend(cfg: VadConfig) -> str:
    """解析实际使用的后端名（auto → silero 可用则 silero，否则 energy）。"""
    if cfg.backend == "auto":
        return "silero" if _silero_available() else "energy"
    return cfg.backend


def vad_segments(audio: np.ndarray, sr: int, cfg: VadConfig | None = None,
                 *, threshold: float | None = None,
                 min_speech_ms: int | None = None,
                 min_silence_ms: int | None = None,
                 speech_pad_ms: int | None = None) -> list[tuple[float, float]]:
    """切出有人声的段（含两侧 pad），返回 [(start_s, end_s), ...]（原始轴）。"""
    from .config import VadConfig as _VC
    cfg = cfg or _VC()
    backend = VadBackend(cfg)
    kwargs = dict(
        threshold=cfg.threshold if threshold is None else threshold,
        min_speech_ms=cfg.min_speech_ms if min_speech_ms is None else min_speech_ms,
        min_silence_ms=cfg.min_silence_ms if min_silence_ms is None else min_silence_ms,
        speech_pad_ms=cfg.speech_pad_ms if speech_pad_ms is None else speech_pad_ms,
    )
    if backend == "silero":
        try:
            return silero_vad_segments(audio, sr, **kwargs)
        except Exception as e:                       # 装了但跑不起来 → 回落
            logger.warning("Silero VAD 失败，回落能量法: %s", e)
    return energy_vad_segments(audio, sr, **kwargs)


def load_audio_for_vad(path: str | Path, sr: int = 16000) -> tuple[np.ndarray, int]:
    """便捷入口：读音频用于 VAD（复用 preprocess 的解码与降级链）。"""
    from .preprocess import load_audio
    return load_audio(path, target_sr=sr)


# ── 能量法（零依赖，默认可用）──────────────────────────────────────────

def energy_vad_segments(audio: np.ndarray, sr: int, *, threshold: float = 0.5,
                        min_speech_ms: int = 250, min_silence_ms: int = 500,
                        speech_pad_ms: int = 200,
                        frame_ms: int = 32) -> list[tuple[float, float]]:
    """自适应能量 VAD。

    思路：分帧算 RMS(dB) → 用「噪声底 + 动态范围 × threshold」自适应定阈值
    （固定阈值在不同录音上必然失效）→ 迟滞判定 → 丢短段 → 并短隙 → 两侧补 pad。

    threshold 的语义：0=只要比噪声底响一点就算语音，1=只有最响的帧才算。
    """
    if audio.size == 0:
        return []
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    frame = max(1, int(sr * frame_ms / 1000))
    n_frames = audio.size // frame
    if n_frames == 0:
        return []

    frames = audio[: n_frames * frame].reshape(n_frames, frame)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1) + 1e-12)
    db = 20.0 * np.log10(rms + 1e-9)

    noise = float(np.percentile(db, 10))
    peak = float(np.percentile(db, 95))
    span = max(peak - noise, 6.0)                    # 下限 6dB，防极安静录音阈值塌缩
    thr = noise + span * float(np.clip(threshold, 0.0, 1.0))
    low = thr - 3.0                                  # 迟滞：低于 thr-3dB 才算静音

    # 迟滞状态机
    voiced = np.zeros(n_frames, dtype=bool)
    on = False
    for i, x in enumerate(db):
        if on:
            if x < low:
                on = False
            else:
                voiced[i] = True
        elif x > thr:
            on = True
            voiced[i] = True

    # 帧 → 段
    raw: list[tuple[int, int]] = []
    start: int | None = None
    for i, v in enumerate(voiced):
        if v and start is None:
            start = i
        elif not v and start is not None:
            raw.append((start, i))
            start = None
    if start is not None:
        raw.append((start, n_frames))

    dur = audio.size / sr
    return _postprocess(raw, frame, sr, dur, min_speech_ms, min_silence_ms,
                        speech_pad_ms)


def _postprocess(raw: list[tuple[int, int]], frame: int, sr: int, dur: float,
                 min_speech_ms: int, min_silence_ms: int,
                 speech_pad_ms: int) -> list[tuple[float, float]]:
    """丢短段 → 并短隙 → 两侧补 pad → 裁剪到音频范围。"""
    min_speech_f = max(0, int(min_speech_ms * sr / 1000 / frame))
    min_silence_s = min_silence_ms / 1000.0
    pad_s = speech_pad_ms / 1000.0

    kept = [(a, b) for a, b in raw if (b - a) >= min_speech_f] or raw[:1]

    merged: list[list[float]] = []
    for a, b in kept:
        s, e = a * frame / sr, b * frame / sr
        if merged and s - merged[-1][1] < min_silence_s:
            merged[-1][1] = e                        # 间隙太短 → 并成一段
        else:
            merged.append([s, e])

    out: list[tuple[float, float]] = []
    for s, e in merged:
        s, e = max(0.0, s - pad_s), min(dur, e + pad_s)
        if e - s > 1e-3:
            out.append((round(s, 3), round(e, 3)))
    return out


# ── Silero（懒加载，有 torch 才可用）──────────────────────────────────

_SILERO_CACHE: dict[str, object] = {}


def _silero_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def silero_vad_segments(audio: np.ndarray, sr: int, *, threshold: float = 0.5,
                        min_speech_ms: int = 250, min_silence_ms: int = 500,
                        speech_pad_ms: int = 200) -> list[tuple[float, float]]:
    """Silero-VAD：逐帧输出语音概率，按阈值 + 最短段长 + 最短间隔合并成段。

    pad 语义与 faster-whisper 的 VadOptions 对齐：**段边界左右各扩
    speech_pad_ms**（即 chunk.start = 真实语音起点 − pad）。
    """
    import torch

    model = _silero_model()
    x = torch.from_numpy(np.asarray(audio, dtype=np.float32).reshape(-1))
    if sr != 16000:
        x = torch.nn.functional.interpolate(
            x.view(1, 1, -1), scale_factor=16000 / sr, mode="linear",
            align_corners=False).view(-1)

    win = 512                                        # 32ms @16k，Silero 默认
    dur = x.numel() / 16000.0
    probs: list[tuple[float, float, float]] = []     # (start_s, end_s, prob)
    for i in range(0, x.numel() - win + 1, win):
        chunk = x[i: i + win]
        with torch.no_grad():
            p = float(model(chunk, 16000).item())
        probs.append((i / 16000.0, (i + win) / 16000.0, p))

    raw: list[tuple[int, int]] = []
    start: int | None = None
    for i, (_, _, p) in enumerate(probs):
        if p >= threshold and start is None:
            start = i
        elif p < threshold - 0.15 and start is not None:   # 迟滞
            raw.append((start, i))
            start = None
    if start is not None:
        raw.append((start, len(probs)))

    return _postprocess(raw, win, 16000, dur, min_speech_ms, min_silence_ms,
                        speech_pad_ms)


def _silero_model():
    if "model" in _SILERO_CACHE:
        return _SILERO_CACHE["model"]
    import torch
    model, _ = torch.hub.load(repo_or_dir="snakers4/silero-vad",
                              model="silero_vad", force_reload=False,
                              trust_repo=True)
    _SILERO_CACHE["model"] = model
    return model
