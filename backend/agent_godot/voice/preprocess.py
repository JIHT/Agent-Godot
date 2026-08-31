"""voice/preprocess.py —— 音频前处理（M16 §9.2 缺口 13）

为什么需要这一节：VAD 阈值和 ASR 都对**音量**敏感。不做响度归一化，会出现
"同一套参数在不同录音上表现迥异"——用户录的轻声样本整段被 VAD 判成静音，
诊断报告直接空掉。

处理链：ffmpeg → 16kHz mono wav → （可选）EBU R128 响度归一 → （可选）高通去 DC。

降级策略（重要）：**没有 ffmpeg 也能跑**——用标准库 wave + numpy 做
WAV 解码/重采样/峰值归一，只是没有响度归一。非 WAV 输入在无 ffmpeg 时
抛 PreprocessError 并给出明确安装提示。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from .config import PreprocessConfig

__all__ = ["PreprocessError", "ffmpeg_available", "load_audio",
           "to_16k_mono", "resample", "peak_normalize"]


class PreprocessError(RuntimeError):
    """音频前处理失败（无 ffmpeg 且输入非 WAV / 文件损坏 / 采样率非法）。"""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# ── 读取 ──────────────────────────────────────────────────────────────

def load_audio(path: str | Path, target_sr: int = 16000) -> tuple[np.ndarray, int]:
    """读音频为 float32 单声道数组，并按需重采样到 target_sr。

    有 ffmpeg 走 ffmpeg（支持 mp3/m4a/flac…）；否则用 wave 标准库（仅 wav），
    重采样退化为线性插值（够 VAD/ASR 用，非高保真场景）。
    """
    path = Path(path)
    if not path.exists():
        raise PreprocessError(f"音频文件不存在: {path}")

    if ffmpeg_available():
        data = _decode_ffmpeg(path, target_sr)
        if data is not None:
            return data.astype(np.float32, copy=False), target_sr

    return _decode_wave_stdlib(path, target_sr)


def _decode_ffmpeg(path: Path, target_sr: int) -> np.ndarray | None:
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
           "-f", "s16le", "-ac", "1", "-ar", str(target_sr), "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    if not raw:
        return None
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _decode_wave_stdlib(path: Path, target_sr: int) -> tuple[np.ndarray, int]:
    """零依赖 WAV 解码（仅 PCM）。非 WAV 会抛出带安装提示的错误。"""
    try:
        with wave.open(str(path), "rb") as w:
            sr = w.getframerate()
            ch = w.getnchannels()
            width = w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise PreprocessError(
            f"无法解码音频 {path.name}：{e}。当前环境未安装 ffmpeg，"
            f"仅支持 PCM WAV；安装 ffmpeg 后可处理 mp3/m4a/flac 等格式") from e

    if width != 2:
        raise PreprocessError(
            f"仅支持 16-bit PCM WAV（{path.name} 是 {width * 8}-bit）。"
            f"安装 ffmpeg 后可自动转码")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:                                   # 多声道 → 均值混合成单声道
        data = data.reshape(-1, ch).mean(axis=1)
    if sr != target_sr:
        data = resample(data, sr, target_sr)
        sr = target_sr
    return data.astype(np.float32, copy=False), sr


def resample(data: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """线性插值重采样（无 scipy 依赖）。VAD/ASR 场景够用。"""
    if src_sr == dst_sr or data.size == 0:
        return data
    n = int(round(data.size * dst_sr / src_sr))
    if n <= 1:
        return data[:1]
    src_idx = np.linspace(0.0, data.size - 1, num=n, dtype=np.float64)
    return np.interp(src_idx, np.arange(data.size, dtype=np.float64),
                     data.astype(np.float64)).astype(np.float32)


def peak_normalize(data: np.ndarray, target_peak: float = 0.95) -> np.ndarray:
    """峰值归一（ffmpeg 缺失时的响度归一替代品）。"""
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    if peak < 1e-6:
        return data
    return (data * (target_peak / peak)).astype(np.float32)


# ── 转码 ──────────────────────────────────────────────────────────────

def to_16k_mono(src: str | Path, cfg: PreprocessConfig | None = None,
                *, out_dir: str | Path | None = None) -> Path:
    """统一转码为 16kHz 单声道 WAV，返回可直接喂 ASR 的路径。

    幂等：输入已是 16k mono wav 且未开启响度归一 → 原样返回，不产生临时文件。
    """
    from .config import PreprocessConfig as _PC
    cfg = cfg or _PC()
    src = Path(src)
    if not src.exists():
        raise PreprocessError(f"音频文件不存在: {src}")

    if not cfg.enabled:
        return src

    if ffmpeg_available():
        return _transcode_ffmpeg(src, cfg, out_dir)

    # 降级路径：仅 WAV，用标准库 + numpy
    data, sr = _decode_wave_stdlib(src, cfg.target_sr)
    if cfg.loudnorm:
        data = peak_normalize(data)
    return _write_temp_wav(data, sr, out_dir)


def _transcode_ffmpeg(src: Path, cfg: PreprocessConfig,
                      out_dir: str | Path | None) -> Path:
    filters = [f"highpass=f={cfg.highpass_hz}"] if cfg.highpass_hz > 0 else []
    if cfg.loudnorm:
        # EBU R128：双 pass 太慢，单 pass 动态归一足够工程用
        filters.append(f"loudnorm=I={cfg.target_lufs}:TP=-1.5:LRA=11")
    af = ",".join(filters) if filters else "anull"

    out = _tmp_path(out_dir, src.stem)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
           "-af", af, "-ac", "1", "-ar", str(cfg.target_sr), "-c:a", "pcm_s16le",
           str(out)]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
        raise PreprocessError(f"ffmpeg 转码失败: {e}") from e
    return out


def _tmp_path(out_dir: str | Path | None, stem: str) -> Path:
    d = Path(out_dir) if out_dir else Path(tempfile.gettempdir()) / "agent-godot-voice"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{stem}.16k.wav"


def _write_temp_wav(data: np.ndarray, sr: int, out_dir: str | Path | None) -> Path:
    pcm = np.clip(data, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    out = _tmp_path(out_dir, "audio")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return out
