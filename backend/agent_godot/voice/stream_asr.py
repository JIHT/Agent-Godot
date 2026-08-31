"""voice/stream_asr.py —— 流式转写（M16 §1.5 · §9.2 缺口 9）

核心问题：Whisper 类模型是**离线模型**（吃完整输入），流式要靠**同时策略**
（simultaneous policy）——用离线模型配一个"什么时候敢吐字"的算法。

三种策略（SimulStreaming 论文结论，IWSLT 2025 同传赛道 SOTA）：

| 策略 | 机制 | 大白话 |
|---|---|---|
| **AlignAtt**（质量最好） | 用 encoder-decoder 交叉注意力判断"当前解码到源音频哪一帧"，注意力一进入"危险区"（接近缓冲区末尾）就停止解码，等下一块 | **看着稿子念**：念到稿子边缘就停，等新一页送来 |
| **LocalAgreement**（次优，易实现） | 取相邻两次更新输出的**最长公共前缀**作为 confirmed | **两次说法一致才算数** |
| 滑窗（本模块原方案，已弃） | 每来一块重跑整窗再比对 | 算力浪费 + 长句收敛慢 |

★ §1.5 铁律不变：**partial 只供热身，落史 / 执行工具必须等 final**。
  策略只是让 final 来得更早、更可信，**不改变"final 才可提交"这条红线**。

AlignAtt 需要模型暴露交叉注意力（SimulStreaming / 原生流式 ASR 才有）。
本模块的 `ASRBackend` 抽象没暴露这一层，所以 `AlignAttPolicy` 的落地策略是：
**后端支持原生流式则委托，否则回落 LocalAgreement 并警告**——
诚实地降级，而不是假装实现了 AlignAtt。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from .schema import AsrDelta

logger = logging.getLogger(__name__)

__all__ = ["StreamingPolicy", "LocalAgreementPolicy", "AlignAttPolicy",
           "StreamingTranscriber", "LocalStreamTranscriber",
           "FileStreamTranscriber", "make_policy", "tokenize"]


def tokenize(text: str, lang: str = "zh") -> list[str]:
    """分词：中文按字符，英文按空格（LocalAgreement 的对齐单位）。"""
    if lang.lower().startswith("en"):
        return text.split()
    return [c for c in text if not c.isspace()]


# ── 同时策略 ──────────────────────────────────────────────────────────

class StreamingPolicy(ABC):
    """决定"什么时候敢吐字"。"""

    name: str = "abstract"

    def reset(self) -> None:
        pass

    @abstractmethod
    def update(self, tokens: list[str]) -> tuple[str, str]:
        """喂入本次更新的 token 序列，返回 (confirmed, partial)。"""

    def flush(self) -> tuple[str, str]:
        """音频结束时把剩余部分定为 final。"""
        return "", ""


class LocalAgreementPolicy(StreamingPolicy):
    """取相邻两次更新输出的最长公共前缀作为 confirmed，其余可被推翻。"""

    name = "localagreement"

    def __init__(self, join: str = "") -> None:
        self.join = join
        self.prev: list[str] = []
        self.confirmed: list[str] = []
        self.partial: list[str] = []

    def reset(self) -> None:
        self.prev, self.confirmed, self.partial = [], [], []

    def update(self, tokens: list[str]) -> tuple[str, str]:
        n = 0
        for a, b in zip(self.prev, tokens):
            if a != b:
                break
            n += 1
        # 新确认的 = 本次共识中尚未确认的部分
        if n > len(self.confirmed):
            self.confirmed = tokens[:n]
        self.partial = tokens[len(self.confirmed):]
        self.prev = tokens
        return self.join.join(self.confirmed), self.join.join(self.partial)

    def flush(self) -> tuple[str, str]:
        """结束时全部转正确认（末词可能被截断，但总比丢失强）。"""
        if self.partial:
            self.confirmed = self.confirmed + self.partial
            self.partial = []
        return self.join.join(self.confirmed), ""


class AlignAttPolicy(StreamingPolicy):
    """AlignAtt：优先委托给支持原生流式的后端，否则回落 LocalAgreement。

    真正的 AlignAtt 需要读解码器的交叉注意力（"解码到音频第几帧了"），
    这是模型内部状态。后端不暴露时，用 LocalAgreement 顶上是**诚实的降级**。
    """

    name = "alignatt"

    def __init__(self, backend=None, lang: str = "zh", join: str = "") -> None:
        self.backend = backend
        self.lang = lang
        self.join = join
        self._fallback = LocalAgreementPolicy(join=join)
        self._warned = False

    def reset(self) -> None:
        self._fallback.reset()

    def _native(self) -> bool:
        return self.backend is not None and hasattr(self.backend, "stream_step")

    def update(self, tokens: list[str]) -> tuple[str, str]:
        if self._native():
            return self.backend.stream_step(tokens)      # type: ignore[attr-defined]
        if not self._warned:
            logger.warning(
                "后端 %s 不提供原生 AlignAtt（无 stream_step），"
                "回落 LocalAgreement 策略",
                getattr(self.backend, "name", "?"))
            self._warned = True
        return self._fallback.update(tokens)

    def flush(self) -> tuple[str, str]:
        if self._native():
            return self.backend.stream_flush()           # type: ignore[attr-defined]
        return self._fallback.flush()


def make_policy(name: str, *, backend=None, lang: str = "zh") -> StreamingPolicy:
    if name == "alignatt":
        return AlignAttPolicy(backend=backend, lang=lang)
    return LocalAgreementPolicy()


# ── 流式转写器 ────────────────────────────────────────────────────────

class StreamingTranscriber(ABC):
    """音频块进、增量文本出。partial 只供热身，final 才可入史（§1.5 铁律）。"""

    @abstractmethod
    async def feed(self, pcm: bytes) -> AsyncIterator[AsrDelta]: ...

    @abstractmethod
    async def finish(self) -> AsyncIterator[AsrDelta]:
        """音频结束（VAD 判端点）→ 冲刷出 final。"""

    async def aclose(self) -> None:
        pass


class LocalStreamTranscriber(StreamingTranscriber):
    """本地流式：缓冲累积 PCM，每隔 min_chunk_s 用**整段缓冲**跑一次 ASR，
    再交给策略决定 confirmed/partial。

    这是 WhisperStreaming 的编排形态（策略可换成 AlignAtt）。算力上不如
    AlignAtt 省，但**零模型改造**就能跑通任何离线后端。
    """

    def __init__(self, backend, *, lang: str = "zh", sr: int = 16000,
                 min_chunk_s: float = 1.0,
                 policy: StreamingPolicy | None = None) -> None:
        self.backend = backend
        self.lang = lang
        self.sr = sr
        self.min_chunk_s = min_chunk_s
        self.policy = policy or make_policy("localagreement", lang=lang)
        self._buf = bytearray()
        self._elapsed = 0.0

    async def feed(self, pcm: bytes) -> AsyncIterator[AsrDelta]:
        self._buf.extend(pcm)
        self._elapsed += len(pcm) / 2 / self.sr          # s16le 单声道
        if self._elapsed < self.min_chunk_s:
            return
        async for d in self._drain(is_final=False):
            yield d

    async def finish(self) -> AsyncIterator[AsrDelta]:
        async for d in self._drain(is_final=True):
            yield d
        self._buf.clear()

    async def _drain(self, *, is_final: bool) -> AsyncIterator[AsrDelta]:
        import tempfile
        from pathlib import Path

        if not self._buf:
            return
        # ★ 计时器必须归零：否则过了第一个 min_chunk_s 之后每来一帧都要
        #   重跑整段缓冲（O(n²) 算力，长音频直接卡死）
        self._elapsed = 0.0
        import wave

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = Path(f.name)
        try:
            with wave.open(str(tmp), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.sr)
                w.writeframes(bytes(self._buf))
            from .config import AsrConfig
            tr = await self.backend.transcribe(tmp, lang=self.lang,
                                               cfg=AsrConfig())
        finally:
            tmp.unlink(missing_ok=True)

        tokens = tokenize(tr.text, self.lang)
        if is_final:
            confirmed, partial = self.policy.flush()
            if partial:
                confirmed = confirmed + partial
            text = confirmed or "".join(tokens)
        else:
            confirmed, partial = self.policy.update(tokens)
            text = partial
        if text:
            yield AsrDelta(text=text, is_final=is_final,
                           end=self._elapsed, confirmed=confirmed)


class FileStreamTranscriber(StreamingTranscriber):
    """从文件模拟流式输入（测试 / 离线对拍 / 延迟测量）。

    realtime=False 时不等真实时间，全速喂完——单测里用它跑通整条链路。
    """

    def __init__(self, wav: str | Path, *, chunk_ms: int = 200,
                 realtime: bool = False, sr: int = 16000) -> None:
        self.wav = Path(wav)
        self.chunk_ms = chunk_ms
        self.realtime = realtime
        self.sr = sr

    async def feed(self, pcm: bytes) -> AsyncIterator[AsrDelta]:
        return
        yield                                            # pragma: no cover

    async def finish(self) -> AsyncIterator[AsrDelta]:
        return
        yield                                            # pragma: no cover

    async def chunks(self) -> AsyncIterator[bytes]:
        import asyncio

        import numpy as np
        from .preprocess import load_audio

        audio, sr = load_audio(self.wav, target_sr=self.sr)
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        size = int(sr * self.chunk_ms / 1000) * 2
        for i in range(0, len(pcm), size):
            if self.realtime:
                await asyncio.sleep(self.chunk_ms / 1000.0)
            yield pcm[i: i + size]
