"""voice/tts.py —— 流式语音合成（M16 §1.4 · §9.2 缺口 11）

形态：**分句流式**——LLM 流式输出按标点切句，逐句送合成、逐块播放。
**唯一 KPI 是首音时延**（TTFA：首块音频可播时刻 − 用户说完时刻），
不是总合成时长。人一旦听到声音，耐心计时器就重置（§1.4 ②）。

分句双阈值（§1.4 ⑤）：韵律边界（。！？；）优先 + **长度兜底**
（18 字强制切，防长句饿死首音——等"。"再切，首句可能 40 字）。

三个后端：
- **Qwen3-TTS-0.6B**：10 语言 / 3 秒零样本克隆 / **97ms 流式** / Apache 2.0
  / 约 4GB VRAM —— 中文场景的本地首选
- **Kokoro-82M**：4090 上 210x 实时 / 54 预设音色 / Apache 2.0 —— 轻量备选
- **edge-tts**：云端兜底（免费、无需 GPU，但有网络往返与限流风险）

★ 采样率三处一致（合成器/播放器/录音器）——48k 合成 16k 播放 = 变速怪声。
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

from .config import TtsConfig
from .schema import TtsChunk

logger = logging.getLogger(__name__)

__all__ = ["Synthesizer", "EdgeTtsSynthesizer", "QwenTtsSynthesizer",
           "KokoroSynthesizer", "MockSynthesizer", "SentenceSplitter",
           "sentences_of", "pronounce_code", "build_synthesizer",
           "synthesize_all"]

# 韵律边界（优先切）+ 长度兜底（强制切）
_SENT_END = "。！？!?；;\n"
_CLAUSE = "，,、：:）)】」"

# 代码符号读法：player.gd → "player dot G D"（§1.4 ⑤）
_CODE_EXT = re.compile(r"\b([A-Za-z_][\w]*)\.(gd|gdscript|tscn|tres|py|json|cfg|md)\b")
_DOTTED = re.compile(r"\b([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)\b")
_CAMEL = re.compile(r"\b([A-Z]+[a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")


def pronounce_code(text: str) -> str:
    """把代码符号替换成 TTS 读得出来的形式。

    坑（§1.4 ⑤）："player.gd" 会被读成拼音。"GDScript" 会被读成一个英文字
    的胡乱拼读。这里做词表正则预替换——**合成前的文本层修复，比换模型便宜**。
    """
    if not text:
        return text

    def _ext(m: re.Match[str]) -> str:
        return f"{m.group(1)} dot {m.group(2)}"

    text = _CODE_EXT.sub(_ext, text)
    text = _DOTTED.sub(lambda m: m.group(1).replace(".", " dot "), text)
    text = _CAMEL.sub(lambda m: re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", m.group(1)), text)
    return text


class SentenceSplitter:
    """分句器：韵律边界优先 + 长度兜底。增量文本进、整句出。"""

    def __init__(self, max_chars: int = 18) -> None:
        self.max_chars = max_chars
        self._buf = ""

    def push(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        while self._buf:
            idx = self._first_boundary()
            if idx is not None:
                out.append(self._buf[: idx + 1])
                self._buf = self._buf[idx + 1:]
                continue
            if len(self._buf) >= self.max_chars:      # 长度兜底，防饿死首音
                out.append(self._buf[: self.max_chars])
                self._buf = self._buf[self.max_chars:]
                continue
            break
        return out

    def flush(self) -> list[str]:
        out = [self._buf] if self._buf.strip() else []
        self._buf = ""
        return out

    def _first_boundary(self) -> int | None:
        """返回第一个"值得切"的位置：优先句末标点，其次（够长时）句内停顿。"""
        for i, c in enumerate(self._buf):
            if c in _SENT_END:
                return i
        if len(self._buf) >= self.max_chars:
            for i, c in enumerate(self._buf):
                if c in _CLAUSE and i >= self.max_chars // 2:
                    return i
        return None


def sentences_of(text: str, max_chars: int = 18) -> list[str]:
    """一次性分句（离线场景）。"""
    sp = SentenceSplitter(max_chars)
    return sp.push(text) + sp.flush()


# ── 合成器 ────────────────────────────────────────────────────────────

async def _iter_sentences(sentences: AsyncIterator[str] | Iterable[str]
                           ) -> AsyncIterator[str]:
    """统一句子来源：异步生成器（流式）与列表（离线）都吃。

    ★ 必须**惰性消费**——若在这里 `list(sentences)`，流式就退化成"等 LLM
      全部说完才开始合成"，首音时延直接被 LLM 总时长吃掉（§1.4 的分句流式
      意义全失）。
    """
    if isinstance(sentences, (list, tuple)):
        for s in sentences:
            yield s
        return
    if hasattr(sentences, "__aiter__"):
        async for s in sentences:
            yield s
        return
    for s in sentences:                          # 普通生成器/迭代器
        yield s


class Synthesizer(ABC):
    """逐句吃文本、逐块吐音频。首音时延是唯一 KPI。"""

    name: str = "abstract"
    sample_rate: int = 24000

    @abstractmethod
    async def synthesize_stream(self, sentences: AsyncIterator[str] | Iterable[str],
                                **kw) -> AsyncIterator[TtsChunk]: ...

    async def aclose(self) -> None:
        pass


class EdgeTtsSynthesizer(Synthesizer):
    """edge-tts：云端兜底，零 GPU。逐句请求，拿到即吐块。"""

    name = "edge-tts"

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+0%",
                 sample_rate: int = 24000, **kw) -> None:
        self.voice = voice
        self.rate = rate
        self.sample_rate = sample_rate

    async def synthesize_stream(self, sentences, **kw) -> AsyncIterator[TtsChunk]:
        try:
            import edge_tts
        except ImportError as e:
            raise RuntimeError(
                "未安装 edge-tts。安装：uv pip install edge-tts"
            ) from e

        seq = 0
        async for s in _iter_sentences(sentences):
            s = s.strip()
            if not s:
                continue
            comm = edge_tts.Communicate(s, self.voice, rate=self.rate)
            buf = bytearray()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
                    # 攒到 8KB 或该句结束才吐，避免过碎的小块
                    if len(buf) >= 8192:
                        yield TtsChunk(seq=seq, audio=bytes(buf), text=s)
                        seq += 1
                        buf.clear()
            if buf:
                yield TtsChunk(seq=seq, audio=bytes(buf), text=s)
                seq += 1
        yield TtsChunk(seq=seq, audio=b"", is_final=True)


class QwenTtsSynthesizer(Synthesizer):
    """Qwen3-TTS-0.6B：本地流式，97ms 首音（中文首选）。"""

    name = "qwen3-tts-0.6b"

    def __init__(self, model: str = "Qwen/Qwen3-TTS-0.6B", voice: str = "",
                 sample_rate: int = 24000, device: str = "cuda:0", **kw) -> None:
        self.model = model
        self.voice = voice
        self.sample_rate = sample_rate
        self.device = device
        self._engine = None

    def _load(self):
        if self._engine is not None:
            return self._engine
        try:
            from qwen_tts import QwenTTS
        except ImportError as e:
            raise RuntimeError(
                "未安装 qwen-tts。安装：uv pip install qwen-tts（需 GPU）"
            ) from e
        self._engine = QwenTTS(self.model, device=self.device)
        return self._engine

    async def synthesize_stream(self, sentences, **kw) -> AsyncIterator[TtsChunk]:
        import asyncio

        engine = self._load()
        seq = 0
        async for s in _iter_sentences(sentences):
            s = s.strip()
            if not s:
                continue
            audio = await asyncio.to_thread(engine.synthesize, s, voice=self.voice)
            yield TtsChunk(seq=seq, audio=bytes(audio), text=s)
            seq += 1
        yield TtsChunk(seq=seq, audio=b"", is_final=True)


class KokoroSynthesizer(Synthesizer):
    """Kokoro-82M：轻量本地（210x 实时），无克隆，54 预设音色。"""

    name = "kokoro"

    def __init__(self, voice: str = "af_heart", sample_rate: int = 24000, **kw):
        self.voice = voice
        self.sample_rate = sample_rate

    async def synthesize_stream(self, sentences, **kw) -> AsyncIterator[TtsChunk]:
        import asyncio

        try:
            from kokoro import KPipeline
        except ImportError as e:
            raise RuntimeError("未安装 kokoro。安装：uv pip install kokoro") from e
        pipe = KPipeline(lang_code="z" if self.voice.startswith("z") else "a")
        seq = 0
        async for s in _iter_sentences(sentences):
            s = s.strip()
            if not s:
                continue

            def _run():
                import numpy as np
                parts = [a for _, _, a in pipe(s, voice=self.voice)]
                if not parts:
                    return b""
                return np.concatenate(parts).astype("<f4").tobytes()
            yield TtsChunk(seq=seq, audio=await asyncio.to_thread(_run), text=s)
            seq += 1
        yield TtsChunk(seq=seq, audio=b"", is_final=True)


class MockSynthesizer(Synthesizer):
    """假合成器：按字数生成对应时长的静音（16-bit PCM）。

    存在的意义：让 realtime 全双工链路在**没有 TTS 引擎**的环境下可测。
    seq 连续、is_final 语义、长度比例都与真后端一致。
    """

    name = "mock"

    def __init__(self, sample_rate: int = 24000, chars_per_second: float = 6.0,
                 **kw) -> None:
        self.sample_rate = sample_rate
        self.chars_per_second = chars_per_second

    async def synthesize_stream(self, sentences, **kw) -> AsyncIterator[TtsChunk]:
        seq = 0
        async for s in _iter_sentences(sentences):
            s = s.strip()
            if not s:
                continue
            n = max(1, int(self.sample_rate * len(s) / self.chars_per_second))
            yield TtsChunk(seq=seq, audio=b"\x00" * (n * 2), text=s)
            seq += 1
        yield TtsChunk(seq=seq, audio=b"", is_final=True)


_SYNTHS: dict[str, type[Synthesizer]] = {
    "edge-tts": EdgeTtsSynthesizer,
    "qwen3-tts-0.6b": QwenTtsSynthesizer,
    "qwen-tts": QwenTtsSynthesizer,
    "kokoro": KokoroSynthesizer,
    "kokoro-82m": KokoroSynthesizer,
    "mock": MockSynthesizer,
}


def build_synthesizer(cfg: TtsConfig | None = None, *,
                      name: str | None = None) -> Synthesizer:
    from .config import TtsConfig as _TC
    cfg = cfg or _TC()
    key = (name or cfg.default or "mock").lower()
    cls = _SYNTHS.get(key)
    if cls is None:
        logger.warning("未知 TTS 后端 %r，回落 mock（已知: %s）", key, sorted(_SYNTHS))
        cls = MockSynthesizer
    try:
        return cls(voice=cfg.voice, rate=cfg.rate, sample_rate=cfg.sample_rate)
    except Exception as e:                     # noqa: BLE001
        logger.warning("TTS 后端 %s 初始化失败，回落 mock: %s", key, e)
        return MockSynthesizer(sample_rate=cfg.sample_rate)


async def synthesize_all(synth: Synthesizer, text: str,
                         cfg: TtsConfig | None = None) -> bytes:
    """非流式便捷入口：整段文本 → 完整音频字节（CLI 用）。"""
    from .config import TtsConfig as _TC
    cfg = cfg or _TC()
    out = bytearray()
    async for chunk in synth.synthesize_stream(_aiter(
            _prepare(text, cfg))):
        out.extend(chunk.audio)
    return bytes(out)


def _prepare(text: str, cfg: TtsConfig) -> list[str]:
    if cfg.code_symbol_pronunciation:
        text = pronounce_code(text)
    return sentences_of(text, cfg.sentence_max_chars)


async def _aiter(items: list[str]) -> AsyncIterator[str]:
    for s in items:
        yield s
