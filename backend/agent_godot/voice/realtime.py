"""voice/realtime.py —— 全双工会话状态机（M16 §1.6 · §3.2）

三态：LISTENING（收音+端点检测）→ THINKING（LLM 推理+工具执行）→
SPEAKING（TTS 播放+持续监听打断）→ 回 LISTENING。

三个"同时"（§3.2）：
1. **同时收音与播放**（全双工）——对讲机是半双工，电话是全双工
2. **同时跑 ASR 与 LLM**（重叠预热）——partial 一到 LLM 就热身
3. **同时播 TTS 与监听打断**（barge-in）——说错方向时剩下的音频是负担

架构要点：**主循环只负责收音与打断判定，说话跑在独立任务里**。
主循环每帧都要回到 `async for frame in mic`，所以 `_speak()` 必须是
后台任务——任何一路 await 卡住，另两路的实时性立刻破功。

打断的三件套（§1.6 ③）：
- cancel 播放 + **flush 播放器缓冲**（否则用户听到漏出的半句）
- **上轮发言截断到实际播到处**（防 LLM 以为自己说完了 → 上下文错位）
- 新输入以"（用户打断：…）"衔接

★ 掐断不做淡出——淡出的 200ms 会盖住用户开口的第一个字（§1.6 ③）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .config import RealtimeConfig
from .schema import TtsChunk
from .turn import HysteresisTurnDetector, build_turn_detector

logger = logging.getLogger(__name__)

__all__ = ["RealtimeState", "RealtimeSession", "energy_vad_prob"]

Sink = Callable[[TtsChunk], Awaitable[None] | None]
LLMStream = Callable[[str], AsyncIterator[str]]


class RealtimeState(str, Enum):
    LISTENING = "listening"      # 收音 + 端点检测
    THINKING = "thinking"        # LLM 推理 + 工具执行（收音进预输入队列）
    SPEAKING = "speaking"        # TTS 播放 + 持续监听打断


def energy_vad_prob(frame: bytes, threshold_db: float = -45.0) -> float:
    """零依赖 VAD 概率代理：用帧 RMS 映射成 0~1（供打断检测用）。

    生产环境用浏览器 getUserMedia 的 echoCancellation + 服务端 Silero；
    这里是**兜底**——§1.6 说"服务端只做 AEC 失效兑底"，本函数即兑底之一。
    """
    import math

    import numpy as np
    if not frame:
        return 0.0
    x = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
    if x.size == 0:
        return 0.0
    rms = float(np.sqrt((x.astype(np.float64) ** 2).mean() + 1e-12))
    db = 20 * math.log10(rms + 1e-9)
    return float(min(1.0, max(0.0, (db - threshold_db) / 25.0)))


@dataclass
class _Turn:
    """一轮对话的记录（供截断与历史使用）。"""
    user: str = ""
    assistant: str = ""
    chunk_texts: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.perf_counter)


class RealtimeSession:
    """一条 WS 连接一个会话：全双工收放音，端点检测/重叠流水线/打断都在这。"""

    def __init__(self, *, asr, tts, llm_stream: LLMStream, sink: Sink,
                 turn_detector=None, vad_fn: Callable[[bytes], float] | None = None,
                 cfg: RealtimeConfig | None = None, sr: int = 16000,
                 frame_ms: int = 20, vad_threshold: float = 0.5,
                 on_warmup: Callable[[str], Awaitable[None]] | None = None):
        from .config import RealtimeConfig as _RC
        self.asr = asr
        self.tts = tts
        self.llm_stream = llm_stream
        self.sink = sink
        self.cfg = cfg or _RC()
        self.turn_detector = turn_detector or build_turn_detector(
            semantic=self.cfg.semantic_endpoint,
            silence_ms=self.cfg.endpoint_silence_ms,
            min_utterance_ms=self.cfg.min_utterance_ms)
        self.vad_fn = vad_fn or energy_vad_prob
        self.sr = sr
        self.frame_ms = frame_ms
        self.vad_threshold = vad_threshold
        self.on_warmup = on_warmup

        self.state = RealtimeState.LISTENING
        self.history: list[dict[str, str]] = []
        self._turn_pcm = bytearray()
        self._silence_ms = 0.0
        self._utterance_ms = 0.0
        self._partial = ""
        self._speak_task: asyncio.Task | None = None
        self._cur = _Turn()
        self._pending_input: list[str] = []      # THINKING 态收到的预输入
        self._ttfa_ms: float | None = None

    # ---------- 主循环 ----------

    async def run(self, mic: AsyncIterator[bytes],
                  sink: Sink | None = None) -> None:
        sink = sink or self.sink
        async for frame in mic:
            prob = self.vad_fn(frame)
            voiced = prob >= self.vad_threshold
            if voiced:
                self._utterance_ms += self.frame_ms
                self._silence_ms = 0.0
            else:
                self._silence_ms += self.frame_ms

            # ① 播放中：优先判打断（打断优先级高于一切）
            if self._speak_task is not None and not self._speak_task.done():
                if self.state is RealtimeState.SPEAKING and await self._barge_in(frame):
                    await self._cancel_speaking(sink)
                    self._reset_turn()
                continue

            # ② 收音：送 ASR（LISTENING 与 THINKING 都在收，防丢话）
            self._turn_pcm.extend(frame)
            partial_changed = False
            async for delta in self.asr.feed(frame):
                if delta.is_final or not delta.text.strip():
                    continue
                if delta.text != self._partial:
                    partial_changed = True
                    self._partial = delta.text
                # ③ 重叠：partial 只预热不提交（§1.5 铁律）
                if self.on_warmup is not None:
                    await self.on_warmup(delta.text)

            if self.state is not RealtimeState.LISTENING:
                continue

            # ④ 端点检测：物理迟滞 + （可选）语义层
            complete = await self._endpoint(
                silence_ms=self._silence_ms,
                utterance_ms=self._utterance_ms,
                partial_changed=partial_changed,
                turn_pcm=bytes(self._turn_pcm))
            if not complete:
                continue

            final = await self._collect_final()
            self._reset_turn()
            if not final.strip():
                continue
            self._speak_task = asyncio.create_task(self._speak(final, sink))

        await self._wait_and_close()

    # ---------- 端点 ----------

    async def _endpoint(self, *, silence_ms: float, utterance_ms: float,
                        partial_changed: bool,
                        turn_pcm: bytes | None = None) -> bool:
        return await self.turn_detector.is_complete(
            silence_ms=silence_ms, utterance_ms=utterance_ms,
            partial_changed=partial_changed, turn_pcm=turn_pcm)

    async def _collect_final(self) -> str:
        text = ""
        async for d in self.asr.finish():
            if d.is_final and d.text:
                text = d.text
            elif d.confirmed:
                text = d.confirmed
        return text or self._partial

    def _reset_turn(self) -> None:
        self._turn_pcm.clear()
        self._silence_ms = 0.0
        self._utterance_ms = 0.0
        self._partial = ""

    # ---------- 说话流水线 ----------

    async def _speak(self, final_text: str, sink: Sink) -> None:
        """LLM 流式 → 分句器 → TTS 逐块 → sink。可被 cancel（打断）。"""
        from .tts import SentenceSplitter, pronounce_code

        user_text = final_text
        if self._pending_input:
            user_text = "（用户打断：" + final_text + "）"
            self._pending_input.clear()

        self.state = RealtimeState.THINKING
        self._cur = _Turn(user=user_text)
        self.history.append({"role": "user", "content": user_text})

        splitter = SentenceSplitter(max_chars=self._sentence_max())
        t0 = time.perf_counter()
        self._ttfa_ms = None
        seq = 0
        assistant = ""

        try:
            sent_queue: asyncio.Queue[str] = asyncio.Queue()
            producer_error: list[BaseException] = []

            async def _producer() -> None:
                """★ 哨兵必须在 finally 里无条件投递。

                否则 LLM 一旦抛异常（模型不可用、超时、鉴权失败……），
                消费者 `_sentences()` 会在 `await sent_queue.get()` 上
                **永久阻塞**——表现为整个会话静默卡死，连超时都没有。
                异常本身也要带出去，不能只记日志。
                """
                try:
                    async for delta in self.llm_stream(user_text):
                        for s in splitter.push(delta):   # 韵律边界优先 + 长度兜底
                            await sent_queue.put(s)
                    for s in splitter.flush():
                        await sent_queue.put(s)
                except asyncio.CancelledError:
                    raise
                except BaseException as e:               # noqa: BLE001
                    producer_error.append(e)
                finally:
                    await sent_queue.put("")             # 结束哨兵（无条件）

            producer = asyncio.create_task(_producer())

            async def _sentences() -> AsyncIterator[str]:
                while True:
                    s = await sent_queue.get()
                    if not s:
                        return
                    yield s

            async for chunk in self.tts.synthesize_stream(_sentences()):
                if chunk.is_final:
                    break
                if self.state is RealtimeState.THINKING:
                    self.state = RealtimeState.SPEAKING
                if self._ttfa_ms is None:
                    self._ttfa_ms = (time.perf_counter() - t0) * 1000
                    _record_ttfa(self._ttfa_ms)
                # seq 连续无洞：播放器据此检测丢块
                if chunk.seq != seq:
                    logger.warning("TTS 块序号不连续: 期望 %d 实际 %d", seq, chunk.seq)
                seq = chunk.seq + 1
                self._cur.chunk_texts.append(chunk.text)
                self._cur.assistant += chunk.text
                assistant += chunk.text
                r = sink(chunk)
                if asyncio.iscoroutine(r):
                    await r

            await producer                     # 哨兵保证这里一定会返回
            if producer_error:
                raise producer_error[0]        # LLM 的错要冒出来，不能静默
            self.history.append({"role": "assistant", "content": assistant})
        except asyncio.CancelledError:
            raise
        finally:
            if self.state is not RealtimeState.LISTENING:
                self.state = RealtimeState.LISTENING

    def _sentence_max(self) -> int:
        from .config import TtsConfig
        return TtsConfig().sentence_max_chars

    # ---------- 打断 ----------

    async def _barge_in(self, mic_frame: bytes) -> bool:
        """SPEAKING 态监听：AEC 后仍检测到人声 ≥ barge_in_ms → 打断。"""
        if self.vad_fn(mic_frame) < self.vad_threshold:
            self._voice_ms = 0.0
            return False
        self._voice_ms = getattr(self, "_voice_ms", 0.0) + self.frame_ms
        return self._voice_ms >= self.cfg.barge_in_ms

    async def _cancel_speaking(self, sink: Sink) -> None:
        """三件套：cancel + flush + 截断上轮发言。"""
        task, self._speak_task = self._speak_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:                 # noqa: BLE001
                logger.debug("说话任务取消时异常: %s", e)

        # flush 播放器缓冲（前后端约定：Sink 可实现 aflush）
        aflush = getattr(sink, "aflush", None)
        if aflush is not None:
            r = aflush()
            if asyncio.iscoroutine(r):
                await r
        self.state = RealtimeState.LISTENING
        self._voice_ms = 0.0
        logger.info("打断：已 cancel 播放并清空缓冲")

    async def _truncate_last_turn(self, played_upto: int) -> None:
        """把上轮 Assistant 记录截断到**实际播到处**，防上下文错位（§1.6 ⑤）。

        played_upto = 已播出的块数；未播的第 played_upto..n 块文本丢弃。
        """
        played = "".join(self._cur.chunk_texts[:played_upto])
        if self.history and self.history[-1]["role"] == "assistant":
            if played.strip():
                self.history[-1]["content"] = played
            else:
                self.history.pop()
        self._cur.assistant = played

    # ---------- 收尾 ----------

    async def _wait_and_close(self) -> None:
        if self._speak_task is not None and not self._speak_task.done():
            try:
                await self._speak_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.asr.aclose()
        await self.tts.aclose()

    # ---------- 可观测 ----------

    @property
    def ttfa_ms(self) -> float | None:
        return self._ttfa_ms


def _record_ttfa(ms: float) -> None:
    from . import metrics
    metrics.record("tts", {"ttfa_ms": ms})
