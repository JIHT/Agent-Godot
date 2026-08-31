"""voice/stt.py —— 多引擎 ASR 抽象与编排（M16 §9.4.1 · §1.1）

★ 本模块最重要的一条抽象 ★

为什么必须有它：中文 ASR 的 SOTA 在 2026 年**每半年换一次**。原方案把整个
模块押在 whisper 上，而 whisper 中文 CER 5.14%（会议场景 18.87%）已被国产
方案（0.57%~3.76%）拉开代差。不做这层抽象，每次换代都要重写特征层与诊断层。

设计铁律：**换引擎不改变 `TranscriptionResult` 契约**——只动
`config/voice.yaml` 的 `asr.routing`，上层 features/diagnose/tools/UI 零改动。

编排管线（`Transcriber.transcribe`）：
    preprocess（转码+响度归一）→ backend（VAD+ASR）→ align（强制对齐）
    → normalize（ITN+标点+PII）→ diarize（可选）→ metrics

全部重依赖（faster_whisper / funasr / torch）都是**函数内懒导入**——
没装 torch 的机器上本模块照样能 import、能跑测试、能走 MockBackend。
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .config import AsrConfig, VoiceConfig
from .schema import Provenance, Seg, TranscriptionResult, WordInfo

logger = logging.getLogger(__name__)

__all__ = ["ASRBackend", "WhisperBackend", "FunASRBackend", "RemoteASRBackend",
           "MockBackend", "Transcriber", "BackendUnavailable",
           "build_backends", "ENGINE_FACTORIES"]


class BackendUnavailable(RuntimeError):
    """后端依赖未安装 / 服务不可达。message 里带明确的安装提示。"""


# ── 后端协议 ──────────────────────────────────────────────────────────

class ASRBackend(ABC):
    """声学模型是可替换件——这是本模块的依赖倒置点。"""

    name: str = "abstract"
    supports_streaming: bool = False
    supports_emotion: bool = False

    @abstractmethod
    async def transcribe(self, wav: Path, *, lang: str | None,
                         cfg: AsrConfig) -> TranscriptionResult: ...

    def close(self) -> None:
        """释放模型/连接（可选实现）。"""


# ── faster-whisper（英文 / 多语主力）───────────────────────────────────

class WhisperBackend(ASRBackend):
    """faster-whisper 后端。

    采用 §1.1 ③ 的推荐参数（幻觉三防 + 语言检测 + 热词）：
    - vad_filter=True                     治本：静音从输入物理删除
    - condition_on_previous_text=False    治标：切断跨段幻觉传播
    - hallucination_silence_threshold     第三道防线
    - language=None                       让语言检测真的发生（语速单位依赖它）
    """

    name = "whisper"

    def __init__(self, **params: Any) -> None:
        self.params = params
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise BackendUnavailable(
                "未安装 faster-whisper，无法使用 whisper 后端。"
                "安装：uv pip install faster-whisper（GPU 环境另需 ctranslate2）"
            ) from e
        p = dict(self.params)
        self._model = WhisperModel(
            p.pop("model_size", "large-v3-turbo"),
            device=p.pop("device", "auto"),
            compute_type=p.pop("compute_type", "default"))
        self._extra = p
        return self._model

    async def transcribe(self, wav: Path, *, lang: str | None,
                         cfg: AsrConfig) -> TranscriptionResult:
        import asyncio

        model = self._load()
        kwargs: dict[str, Any] = {
            "language": lang or cfg.language,
            "vad_filter": True,
            "vad_parameters": {
                "min_silence_duration_ms": 500,     # 官方默认 2000 太钝
                "speech_pad_ms": 200,               # 官方默认 400
            },
            "word_timestamps": cfg.word_timestamps,
            "condition_on_previous_text": cfg.condition_on_previous_text,
            "hallucination_silence_threshold": cfg.hallucination_silence_threshold,
        }
        if cfg.hotwords:
            kwargs["hotwords"] = ", ".join(cfg.hotwords)
        kwargs.update(getattr(self, "_extra", {}))
        kwargs = _filter_supported(model.transcribe, kwargs)

        # 模型推理是 CPU/GPU 密集的同步调用 → 放线程池，避免阻塞事件循环
        segments, info = await asyncio.to_thread(model.transcribe, str(wav), **kwargs)

        segs: list[Seg] = []
        for s in segments:
            words = [WordInfo(text=(w.word or "").strip(), start=float(w.start),
                              end=float(w.end), prob=float(getattr(w, "probability", 1.0)))
                     for w in (getattr(s, "words", None) or [])]
            segs.append(Seg(
                start=float(s.start), end=float(s.end), text=s.text, words=words,
                avg_logprob=float(getattr(s, "avg_logprob", 0.0)),
                compression_ratio=float(getattr(s, "compression_ratio", 0.0)),
                no_speech_prob=float(getattr(s, "no_speech_prob", 0.0))))
            # 开了 word_timestamps 时段级时间戳被词级覆盖（faster-whisper
            # restore_speech_timestamps 的行为）——段边界以首尾词为准
            if words:
                segs[-1].start, segs[-1].end = words[0].start, words[-1].end

        return TranscriptionResult(
            language=getattr(info, "language", "") or "",
            duration=float(getattr(info, "duration", 0.0)),
            duration_after_vad=float(getattr(info, "duration_after_vad", 0.0)),
            segments=segs,
            provenance=Provenance(
                engine=self.name, language=getattr(info, "language", ""),
                language_prob=float(getattr(info, "language_probability", 0.0)),
                aligned=False, normalized=False),
        )


# ── FunASR（中文：SenseVoice / Paraformer / FireRedASR）─────────────────

# FunASR 的参数分两拨：**构造参数**（搭管线）与**推理参数**（每次调用）。
# 混传会直接 TypeError 或静默失效——这是接 FunASR 最常见的坑。
_FUNASR_MODEL_KWARGS = {"model", "device", "vad_model", "vad_kwargs",
                        "punc_model", "spk_model", "hub", "disable_update",
                        "ncpu", "ngpu"}
_FUNASR_INFER_KWARGS = {"language", "use_itn", "output_timestamp", "ban_emo_unk",
                        "merge_vad", "merge_length_s", "batch_size_s", "hotword"}


class FunASRBackend(ASRBackend):
    """FunASR 工具包后端，一个实现覆盖多个中文模型。

    model 名决定行为：
    - **iic/SenseVoiceSmall**（默认）→ ASR + **情感** + **音频事件**（234M，10s/70ms）
    - paraformer-zh          → 中文通用，支持流式与时间戳
    - FireRedTeam/FireRedASR2-AED → 中文高精度（AISHELL-1 CER 0.57%）

    两条硬约束（踩过的坑）：
    1. SenseVoice 单次只吃 **≤30s** 音频 → 必须挂 `vad_model=fsmn-vad`
       切段，否则 3 分钟录音只转前 30 秒或直接报错。
    2. SenseVoice 输出**不带标点** → 挂 `punc_model=ct-punc` 补回来，
       否则停顿判断与 LLM 读到的转写都是一坨连读。
    """

    def __init__(self, model: str = "iic/SenseVoiceSmall", **params: Any) -> None:
        self.model = model
        self.params = params
        self._model = None
        self.name = _funasr_engine_name(model)
        self.supports_emotion = "sensevoice" in model.lower()
        # 情感/事件只有 SenseVoice 出；其余引擎静默跳过这两个字段
        self.model_kwargs = {k: v for k, v in params.items()
                             if k in _FUNASR_MODEL_KWARGS}
        self.infer_kwargs = {k: v for k, v in params.items()
                             if k in _FUNASR_INFER_KWARGS}

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from funasr import AutoModel
        except ImportError as e:
            raise BackendUnavailable(
                "未安装 funasr，无法使用中文 ASR 后端（SenseVoice/Paraformer/FireRed）。"
                "安装：uv pip install funasr（GPU 环境另需 torch）"
            ) from e
        self._model = AutoModel(model=self.model, **self.model_kwargs)
        return self._model

    async def transcribe(self, wav: Path, *, lang: str | None,
                         cfg: AsrConfig) -> TranscriptionResult:
        import asyncio

        model = self._load()
        # 新版 funasr 用 generate，老版用 inference；两个都兼容
        call = getattr(model, "generate", None) or getattr(model, "inference")

        kwargs: dict[str, Any] = {
            "input": str(wav),
            "language": lang or "auto",   # SenseVoice 支持 auto/zh/yue/en/ja/ko
            "use_itn": False,             # 我们自己做 ITN（要保住词级时间戳）
        }
        kwargs.update(self.infer_kwargs)
        if cfg.hotwords and "hotword" not in kwargs:
            kwargs["hotword"] = " ".join(cfg.hotwords)
        kwargs = _filter_supported(call, kwargs)

        res = await asyncio.to_thread(call, **kwargs)
        return _parse_funasr(res, self.name, engine=self.model, lang=lang)


def _funasr_engine_name(model: str) -> str:
    """iic/SenseVoiceSmall → sensevoice（引擎名要短且稳定，用于日志与埋点）。"""
    name = model.split("/")[-1].lower()
    for key in ("sensevoice", "paraformer", "firered", "qwen"):
        if key in name:
            return key
    return name


def _parse_funasr(res: Any, name: str, *, engine: str,
                  lang: str | None = None) -> TranscriptionResult:
    """FunASR 输出 → 统一契约。

    SenseVoice 的返回结构：
        [{"key":..., "text": "<|HAPPY|>你好啊<|Speech|>",
          "timestamp": [[s_ms, e_ms], ...]}]

    三处关键处理：
    1. **情感与事件以 `<|TAG|>` 内联在 text 里**——必须抽出来放进契约的
       `emotion` / `events` 字段，否则 LLM 会在报告里读到"<|HAPPY|>"这种噪音。
       注意标签是**大小写混合**的（`<|HAPPY|>` 全大写，但 `<|Speech|>`
       `<|Applause|>` `<|Laughter|>` 不是）——正则只写 `[A-Z_]` 会漏掉后者。
    2. **timestamp 单位是毫秒**，且是**字级**（中文非空格语言，与 WhisperX
       的逐字符对齐策略一致）。
    3. **返回可能是多段列表**。挂了 `vad_model` 后长音频会被切成多个 VAD
       段，FunASR 返回 list，每段自带 text 与 timestamp。只取 `res[0]`
       会**静默丢掉后面所有内容**——3 分钟录音只剩前 30 秒。

    ★ duration 故意留 0：这里是"最后一个字的结束时间"，不是音频总长。
      真正的时长由 Transcriber 从 wav 探得——用 last.end 当 duration 会让
      `axis_warning()` 和 VAD 裁剪率全部失真。
    """
    import re

    items = res if isinstance(res, list) else [res]
    items = [i for i in items if isinstance(i, dict)]
    language = (lang or "").strip() or "zh"

    segs: list[Seg] = []
    for item in items:
        raw_text = str(item.get("text") or "")
        if not raw_text:
            continue
        tags = re.findall(r"<\|([A-Za-z_]+)\|>", raw_text)
        text = re.sub(r"<\|[A-Za-z_]+\|>", "", raw_text).strip()

        emotion = next((t for t in tags if t in _EMOTIONS), None)
        # <|Speech|> 每段都有（它只表示"这段是语音"），作为事件上报是纯噪音
        events = [t for t in tags if t in _AUDIO_EVENTS and t != "Speech"]

        words: list[WordInfo] = []
        for i, pair in enumerate(item.get("timestamp") or []):
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            s, e = float(pair[0]) / 1000.0, float(pair[1]) / 1000.0   # ms → s
            ch = text[i] if i < len(text) else ""
            if ch:
                words.append(WordInfo(text=ch, start=round(s, 3),
                                      end=round(e, 3)))
        seg = Seg(start=0.0, end=0.0, text=text, words=words,
                  emotion=emotion, events=events)
        if words:
            seg.start, seg.end = words[0].start, words[-1].end
        segs.append(seg)

    return TranscriptionResult(
        language=language, duration=0.0,
        segments=segs,
        provenance=Provenance(engine=name, engine_version=engine,
                              language=language, language_prob=1.0))


_EMOTIONS = {"HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED"}
_AUDIO_EVENTS = {"BGM", "Speech", "Applause", "Laughter", "Cough", "Sneeze",
                 "Breath", "Cry", "Sigh", "Silence"}


# ── 远程服务（引擎无关，M02 韧性管道第四次复用）─────────────────────────

class RemoteASRBackend(ASRBackend):
    """常驻 ASR 服务的 HTTP 客户端（§3.1 服务化）。

    为什么服务化（§9.2 更新）：多引擎下进程内加载会爆显存——三个 ASR 引擎 +
    embedding 服务共享一张 GPU，靠容器编排调度才是正解。
    """

    name = "remote"

    def __init__(self, base_url: str = "http://127.0.0.1:8001", **params: Any) -> None:
        self.base_url = base_url.rstrip("/")
        self.params = params

    async def transcribe(self, wav: Path, *, lang: str | None,
                         cfg: AsrConfig) -> TranscriptionResult:
        import httpx
        from ..core.retry import RetryableError, with_retry

        async def _call() -> dict:
            try:
                async with httpx.AsyncClient(timeout=cfg.timeout_s) as c:
                    r = await c.post(
                        f"{self.base_url}/transcribe",
                        files={"file": (Path(wav).name, Path(wav).read_bytes(),
                                        "audio/wav")},
                        data={"language": lang or "",
                              "word_timestamps": str(cfg.word_timestamps).lower(),
                              "hotwords": ",".join(cfg.hotwords)})
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                raise RetryableError(f"ASR 服务不可达: {e}") from e
            if r.status_code == 429 or r.status_code >= 500:
                raise RetryableError(f"ASR 服务 {r.status_code}",
                                     retry_after=_retry_after(r))
            if r.status_code >= 400:
                raise BackendUnavailable(f"ASR 服务 {r.status_code}: {r.text[:200]}")
            return r.json()

        payload = await with_retry(_call, max_retries=cfg.max_retries, base=1.0)()
        return TranscriptionResult.from_api(payload)


def _retry_after(resp) -> float | None:
    v = resp.headers.get("retry-after")
    try:
        return float(v) if v else None
    except ValueError:
        return None


# ── Mock（测试与零依赖演示）────────────────────────────────────────────

class MockBackend(ASRBackend):
    """确定性假后端：把给定文本按音频时长均匀铺开成词级时间戳。

    存在的意义：让 features / diagnose / export / realtime 全链路在**没有
    GPU、没有模型**的环境下可测可演示。时间戳是合成的，但契约是真的。
    """

    name = "mock"
    supports_streaming = True

    def __init__(self, text: str = "大家好我叫小明我做过一个 Godot 项目",
                 gap_s: float = 0.05, pause_every: int = 0,
                 pause_s: float = 2.5, duration: float | None = None):
        self.text = text
        self.gap_s = gap_s
        self.pause_every = pause_every      # 每 N 个词插一个 pause_s 的长停顿
        self.pause_s = pause_s
        self.duration = duration

    async def transcribe(self, wav: Path, *, lang: str | None,
                         cfg: AsrConfig) -> TranscriptionResult:
        from .preprocess import load_audio

        audio, sr = load_audio(wav)
        total = self.duration if self.duration is not None else audio.size / sr

        chars = [c for c in self.text if not c.isspace()]
        if not chars:
            return TranscriptionResult(language="zh", duration=total)

        word_dur = 0.18
        span = max(total - 1.0, len(chars) * (word_dur + self.gap_s) + 1.0)
        # 用真实音频时长反推节奏，保证语速数值有意义
        unit = (span - 1.0) / max(len(chars), 1)

        words: list[WordInfo] = []
        t = 0.5
        for i, c in enumerate(chars):
            words.append(WordInfo(text=c, start=round(t, 3),
                                  end=round(t + word_dur, 3), prob=0.95))
            t += unit
            if self.pause_every and (i + 1) % self.pause_every == 0:
                t += self.pause_s

        segs = [Seg(start=words[0].start, end=words[-1].end,
                    text=self.text, words=words)]
        return TranscriptionResult(
            language=lang or "zh", duration=total, duration_after_vad=total,
            segments=segs,
            provenance=Provenance(engine="mock", language=lang or "zh",
                                  language_prob=1.0))


# ── 引擎工厂 ──────────────────────────────────────────────────────────

def _funasr_factory(default_model: str) -> Any:
    """FunASR 系列工厂：配置里的 `model` 键可覆盖默认模型（换微调版）。"""

    def make(**params: Any) -> FunASRBackend:
        model = params.pop("model", default_model)
        return FunASRBackend(model, **params)

    return make


ENGINE_FACTORIES: dict[str, Any] = {
    "whisper": WhisperBackend,
    "remote": RemoteASRBackend,
    "sensevoice": _funasr_factory("iic/SenseVoiceSmall"),
    "paraformer": _funasr_factory("paraformer-zh"),
    "firered": _funasr_factory("FireRedTeam/FireRedASR2-AED"),
    "firered2-aed": _funasr_factory("FireRedTeam/FireRedASR2-AED"),
    "qwen3-asr-0.6b": _funasr_factory("Qwen/Qwen3-ASR-0.6B"),
    "mock": MockBackend,
}


def build_backends(cfg: AsrConfig) -> dict[str, ASRBackend]:
    """按配置实例化被引用的引擎（懒：只建 routing 里出现的）。"""
    needed = set(cfg.routing.values()) | set(cfg.quality_routing.values()) | {cfg.default}
    backends: dict[str, ASRBackend] = {}
    for name in sorted(needed):
        factory = ENGINE_FACTORIES.get(name)
        if factory is None:
            logger.warning("未知 ASR 引擎 %r，跳过（已知: %s）",
                           name, sorted(ENGINE_FACTORIES))
            continue
        params = dict(cfg.engines.get(name) or {})
        try:
            backends[name] = factory(**params)
        except Exception as e:                       # 构造失败不该拖垮整个模块
            logger.warning("ASR 引擎 %s 初始化失败: %s", name, e)
    if not backends:
        logger.warning("没有可用 ASR 引擎，回落 mock")
        backends["mock"] = MockBackend()
    return backends


def _filter_supported(fn: Any, kwargs: dict) -> dict:
    """把 kwargs 收敛到函数签名支持的参数（不同 faster-whisper 版本参数不同）。"""
    import inspect

    try:
        params = set(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


# ── 编排 ──────────────────────────────────────────────────────────────

class Transcriber:
    """转写编排器：preprocess → backend → align → normalize → diarize → metrics。

    上层（tools / diagnose / CLI）只认这一个入口，不直接接触具体引擎。
    """

    def __init__(self, cfg: VoiceConfig | None = None, *,
                 backends: dict[str, ASRBackend] | None = None,
                 strict_timestamps: bool = False):
        from .config import load_voice_config
        self.cfg = cfg or load_voice_config()
        self.backends = backends if backends is not None else build_backends(self.cfg.asr)
        self.strict_timestamps = strict_timestamps

    # ---------- 引擎选择 ----------

    def pick(self, lang_hint: str | None, *, high_accuracy: bool = False) -> ASRBackend:
        """按语言路由：high_accuracy 走 quality_routing，否则走 routing。"""
        table = self.cfg.asr.quality_routing if high_accuracy else self.cfg.asr.routing
        name = (table.get(lang_hint or "")
                or table.get("default")
                or self.cfg.asr.default)
        if name not in self.backends:
            name = self.cfg.asr.default if self.cfg.asr.default in self.backends \
                else next(iter(self.backends))
        return self.backends[name]

    # ---------- 主入口 ----------

    async def transcribe(self, audio: str | Path, *, lang: str | None = None,
                         high_accuracy: bool = False) -> TranscriptionResult:
        from . import align, diarize, metrics, normalize, preprocess

        t0 = time.perf_counter()
        src = Path(audio)

        wav = (preprocess.to_16k_mono(src, self.cfg.preprocess)
               if self.cfg.preprocess.enabled else src)

        backend = self.pick(lang, high_accuracy=high_accuracy)
        logger.info("ASR 引擎: %s（lang=%s, high_accuracy=%s）",
                    backend.name, lang or "auto", high_accuracy)

        tr = await backend.transcribe(wav, lang=lang, cfg=self.cfg.asr)
        if not tr.duration:
            tr.duration = _probe_duration(wav)

        # 语言置信度兜底——语速单位依赖它（§1.1 ⑤b）
        if tr.provenance.language_prob < self.cfg.asr.min_language_prob and tr.language:
            logger.warning("语言检测置信度低（%.2f < %.2f）：%s，"
                           "语速单位仍按 %s 计算，报告中会标注",
                           tr.provenance.language_prob,
                           self.cfg.asr.min_language_prob, tr.language, tr.language)

        tr = align.align_words(tr, wav, tr.language, self.cfg.align)
        tr = normalize.apply(tr, self.cfg.normalize, self.cfg.features)

        if self.cfg.diarize.enabled:
            tr = diarize.assign_speakers(tr, wav, self.cfg.diarize)

        # 时间戳自检：VAD 偏移的两道防线（§1.1 ⑤a）
        try:
            tr.assert_sane()
        except AssertionError as e:
            if self.strict_timestamps:
                raise
            logger.error("时间戳自检失败（数据可能不可信，特征仅供参考）: %s", e)

        if warn := tr.axis_warning():
            # 不做断言：录音末尾本来就可能真有静音。只记日志，由调用方决定
            # 是继续出报告还是拒绝（tools.py 走的就是"拒绝"这条路）
            logger.error("时间戳坐标轴可疑（特征可能失真）: %s", warn)

        if self.cfg.metrics.enabled:
            elapsed = time.perf_counter() - t0
            metrics.record("transcribe", {
                "engine": tr.provenance.engine,
                "language": tr.language,
                "rtf": elapsed / tr.duration if tr.duration else 0.0,
                "vad_trim_rate": tr.vad_trim_rate,
                "low_confidence": tr.low_confidence,
                "duration_s": tr.duration,
            })
        return tr


def _probe_duration(wav: Path) -> float:
    try:
        from .preprocess import load_audio
        audio, sr = load_audio(wav)
        return audio.size / sr
    except Exception:                                 # noqa: BLE001
        return 0.0
