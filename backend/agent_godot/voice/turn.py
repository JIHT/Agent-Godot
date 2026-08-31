"""voice/turn.py —— 端点检测（M16 §1.5 · §9.2 缺口 10）

端点检测 = 判断"用户这句话说完了没"。它是全链路最难压的一段，因为
**它的原料是"未来的静音"——你只能等它发生**（§1.5 ②）。

两层模型（§9.2 缺口 10 的核心结论：**串联，不是替换**）：

**① 物理层（静音迟滞）**——Silero VAD 判"有没有人声"，静音 ≥ 阈值且
partial 无新词且话长够 → 判完。它能榨的声学信息已经榨干了，但永远分不开：

     「我用的是……」（停顿 700ms 在想词，没说完）
     「我用的是 Godot。」（句末停顿 700ms，说完了）

   两者的声学特征几乎一样，区别在**语义完整性与韵律**（句末降调）。
   **这是物理层的极限，不是调参能解决的。**

**② 语义层（Smart Turn v3.2）**——Whisper-Tiny backbone + 线性分类头，
   8M 参数 / int8 量化 8MB / CPU 10ms、云端 65ms / 23 语言含中文 / BSD-2。
   **直接吃 PCM 而非文本** → 能捕捉文本里没有的韵律线索。

★ 为什么不能只用语义层：Smart Turn 的输入是"一个 turn 的音频"，它需要先
  知道 turn 从哪开始——这仍靠 VAD。**VAD 是它的前置，不是它的替代品。**
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

__all__ = ["TurnDetector", "HysteresisTurnDetector", "SmartTurnV3",
           "TwoStageTurnDetector", "build_turn_detector"]


class TurnDetector(ABC):
    """端点检测：给定当前轮的声学状态，判断是否该交棒给 LLM。"""

    name: str = "abstract"

    @abstractmethod
    async def is_complete(self, *, silence_ms: float, utterance_ms: float,
                          partial_changed: bool,
                          turn_pcm: bytes | None = None) -> bool: ...


class HysteresisTurnDetector(TurnDetector):
    """物理层：静音时长 + 迟滞 + 最短话长。

    三条判据（§1.5 ①）：
    - 静音 ≥ endpoint_silence_ms（换气 200ms 不误切）
    - 话长 ≥ min_utterance_ms（咳嗽/清嗓不误触发）
    - partial 持续有新词时静音再长也不切（说话人在组织语言，不是说完）
    """

    name = "hysteresis"

    def __init__(self, silence_ms: int = 500, min_utterance_ms: int = 250) -> None:
        self.silence_ms = silence_ms
        self.min_utterance_ms = min_utterance_ms

    async def is_complete(self, *, silence_ms: float, utterance_ms: float,
                          partial_changed: bool,
                          turn_pcm: bytes | None = None) -> bool:
        if partial_changed:
            return False                       # 还在出新词 → 一定没说完
        return (silence_ms >= self.silence_ms
                and utterance_ms >= self.min_utterance_ms)


class SmartTurnV3(TurnDetector):
    """语义层：Smart Turn v3.2（音频原生的"说完了没"分类器）。

    输入约定（官方要求）：16kHz 单声道 PCM，上限 8 秒；不足 8 秒时
    **在前面补零**（padding 在前，音频在后）；超过 8 秒取末尾 8 秒。
    若判定期间用户又开口，要用**整个 turn 的音频重跑**，而不是只喂新段。
    """

    name = "smart-turn-v3"

    def __init__(self, model_id: str = "pipecat-ai/smart-turn-v3",
                 max_seconds: float = 8.0) -> None:
        self.model_id = model_id
        self.max_seconds = max_seconds
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            import onnxruntime as ort
        except ImportError as e:
            raise RuntimeError(
                "未安装 onnxruntime，无法启用语义端点检测。"
                "安装：uv pip install onnxruntime（或把 realtime.semantic_endpoint 设 false）"
            ) from e
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(self.model_id, "model.onnx")
        self._model = ort.InferenceSession(path)
        return self._model

    async def is_complete(self, *, silence_ms: float, utterance_ms: float,
                          partial_changed: bool,
                          turn_pcm: bytes | None = None) -> bool:
        if turn_pcm is None:
            return True                        # 没有音频 → 不拦，交回物理层结论
        try:
            import numpy as np

            sess = self._load()
            audio = np.frombuffer(turn_pcm, dtype="<i2").astype(np.float32) / 32768.0
            max_n = int(16000 * self.max_seconds)
            if audio.size > max_n:
                audio = audio[-max_n:]         # 取末尾（保留最近的韵律）
            if audio.size < max_n:
                audio = np.pad(audio, (max_n - audio.size, 0))   # ★ 前面补零
            out = sess.run(None, {"audio": audio[None, :]})
            return bool(float(out[0].reshape(-1)[0]) > 0.5)
        except Exception as e:                 # noqa: BLE001
            logger.warning("语义端点检测失败，回退物理层判定: %s", e)
            return True


class TwoStageTurnDetector(TurnDetector):
    """两层串联：物理层先判静音，**通过后再问语义层**"语义完整了吗"。

    语义层只在静音后被触发（Smart Turn 的设计前提：只在静音期运行，
    所以额外延迟只有 65ms 且只发生一次）。
    """

    name = "two-stage"

    def __init__(self, physical: TurnDetector | None = None,
                 semantic: TurnDetector | None = None,
                 *, enabled: bool = True) -> None:
        self.physical = physical or HysteresisTurnDetector()
        self.semantic = semantic
        self.enabled = enabled and semantic is not None

    async def is_complete(self, *, silence_ms: float, utterance_ms: float,
                          partial_changed: bool,
                          turn_pcm: bytes | None = None) -> bool:
        if not await self.physical.is_complete(
                silence_ms=silence_ms, utterance_ms=utterance_ms,
                partial_changed=partial_changed):
            return False
        if not self.enabled or self.semantic is None:
            return True
        return await self.semantic.is_complete(
            silence_ms=silence_ms, utterance_ms=utterance_ms,
            partial_changed=partial_changed, turn_pcm=turn_pcm)


def build_turn_detector(*, semantic: bool = False, silence_ms: int = 500,
                        min_utterance_ms: int = 250) -> TurnDetector:
    """按配置装配：关闭语义层时就是纯迟滞（零依赖）。"""
    physical = HysteresisTurnDetector(silence_ms, min_utterance_ms)
    if not semantic:
        return physical
    try:
        return TwoStageTurnDetector(physical, SmartTurnV3(), enabled=True)
    except Exception as e:                     # noqa: BLE001
        logger.warning("语义端点检测不可用，仅用物理层: %s", e)
        return physical
