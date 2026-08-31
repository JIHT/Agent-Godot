"""voice/schema.py —— 全模块数据契约（M16 §2）

★★ 本模块最重要的一个文件 ★★

为什么把契约单独拎出来：ASR/TTS/VAD/对齐/分离这五个部件在 2026 年全部处于
高速换代期（中文 ASR 半年一换、TTS 三个月出新版），但下游的特征工程、诊断
报告、工具注册、UI 是稳定的。把稳定的挂在契约上、把易变的藏在协议后，是
**依赖倒置**在这个模块的具体形态（§7 拷打第 17 题）。

两条设计纪律：
1. 必须携带置信度——换引擎后精度会变，没有置信度就无法判断数值可不可信；
2. 必须携带溯源信息——否则报告里的数字无法复核（§1.3"数值引用可回链"）。

零第三方依赖：本文件只用标准库，任何环境都能 import。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class WordInfo:
    """词（中文按字符碎片）级时间戳——全部流利度特征的唯一数据源。"""
    text: str
    start: float                      # 秒，★ 原始音频轴（非 VAD 压缩轴）
    end: float
    prob: float = 1.0                 # 词置信度（<0.15 视为异常词）
    speaker: str | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Seg:
    """一个转写段。"""
    start: float
    end: float
    text: str
    words: list[WordInfo] = field(default_factory=list)
    speaker: str | None = None        # SPEAKER_00 —— 绝不含真实姓名（隐私红线）
    emotion: str | None = None        # SenseVoice 白送：HAPPY/SAD/ANGRY/NEUTRAL
    events: list[str] = field(default_factory=list)   # BGM/Laughter/Applause/Cough
    avg_logprob: float = 0.0
    compression_ratio: float = 0.0    # > 2.4 判疑似幻觉（faster-whisper 默认阈值）
    no_speech_prob: float = 0.0

    @property
    def is_suspect(self) -> bool:
        """疑似幻觉段：压缩比过高 或 平均对数概率过低。"""
        return (self.compression_ratio > 2.4) or (self.avg_logprob < -1.0)


@dataclass
class Provenance:
    """溯源：报告里的每个数字都要能查到"谁在什么条件下算出来的"。"""
    engine: str = "unknown"
    engine_version: str = ""
    language: str = ""
    language_prob: float = 0.0
    aligned: bool = False             # 是否经过强制对齐
    normalized: bool = False          # 是否经过 ITN
    sample_rate: int = 16000


@dataclass
class TranscriptionResult:
    """转写结果——全模块的数据契约，换引擎不得改变它。"""
    language: str = ""
    duration: float = 0.0             # ★ 原始音频总长（不是 VAD 后的）
    duration_after_vad: float | None = None
    segments: list[Seg] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)

    # ---------- 派生视图 ----------

    @property
    def words(self) -> list[WordInfo]:
        """扁平化词序列（按时间排序）。特征计算一律走这里。"""
        ws = [w for s in self.segments for w in s.words]
        ws.sort(key=lambda w: w.start)
        return ws

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.segments)

    @property
    def language_prob(self) -> float:
        return self.provenance.language_prob

    @property
    def low_confidence(self) -> bool:
        """整份转写是否含低置信片段（报告里应标注"数值仅供参考"）。"""
        if any(s.is_suspect for s in self.segments):
            return True
        return any(w.prob < 0.15 for w in self.words)

    @property
    def vad_trim_rate(self) -> float:
        """VAD 裁剪率：可观测指标之一（§9.2 可观测性）。"""
        if not self.duration or self.duration_after_vad is None:
            return 0.0
        return 1.0 - (self.duration_after_vad / self.duration)

    # ---------- 自检（§1.1 ⑤a 的守门员） ----------

    def axis_warning(self, tolerance_s: float = 0.5) -> str | None:
        """诊断：时间戳是否**仍停留在 VAD 拼接轴**上。正常返回 None。

        判据：拼接轴的总长恒等于 `duration_after_vad`。所以——
        - 末词时间戳 **>** duration_after_vad + 容差 → 一定在原始轴 ✓
        - 末词时间戳 ≤ duration_after_vad，且 VAD 确实剪掉了可观比例的音频
          → **高度可疑**（VAD 把中间的静音删了，末尾坐标自然也缩水了）

        为什么不做成断言：录音末尾本来就可能有一段真空静音（说话人说完就
        停了），那时 `last.end ≈ duration_after_vad` 是**正常的**。所以这里
        给的是"告警"而非"错误"——由调用方决定是记日志还是拒绝出报告。
        """
        ws = self.words
        if not ws or not self.duration or self.duration_after_vad is None:
            return None
        last = ws[-1].end
        if last > self.duration_after_vad + tolerance_s:
            return None
        trimmed = (self.duration - self.duration_after_vad) / self.duration
        if trimmed <= 0.05:              # VAD 几乎没剪东西 → 两轴本就接近
            return None
        return (f"末词时间戳 {last:.2f}s ≤ duration_after_vad "
                f"{self.duration_after_vad:.2f}s，而 VAD 剪掉了 {trimmed:.0%} 的音频"
                f"——时间戳很可能仍在 VAD 拼接轴上（§1.1 ⑤a）")

    def assert_sane(self, *, min_voiced_ratio: float = 0.02,
                    max_voiced_ratio: float = 0.995) -> None:
        """时间戳健全性自检。VAD 拼接后若未还原，静音被抽干 → 有声占比飙升。

        这是 §1.1 坑 1（VAD 偏移）最便宜的回归防线：宁可断言失败，
        也不要把错坐标喂给特征层。
        """
        ws = self.words
        if not ws:
            return
        for a, b in zip(ws, ws[1:]):
            if b.start < a.end - 1e-3:
                raise AssertionError(
                    f"时间戳非单调/重叠: {a.text!r}[{a.end:.3f}] → "
                    f"{b.text!r}[{b.start:.3f}]")
        if ws[0].start < -1e-3:
            raise AssertionError(f"出现负时间戳: {ws[0].start}")
        if self.duration and ws[-1].end > self.duration + 0.5:
            raise AssertionError(
                f"末词超出音频总长: {ws[-1].end:.3f} > {self.duration:.3f}")
        span = ws[-1].end - ws[0].start
        if span > 0:
            voiced = sum(w.duration for w in ws)
            ratio = voiced / span
            if not (min_voiced_ratio <= ratio <= max_voiced_ratio):
                raise AssertionError(
                    f"有声占比 {ratio:.3f} 越界 [{min_voiced_ratio}, "
                    f"{max_voiced_ratio}] —— 时间戳多半仍停留在 VAD 压缩轴上")

    # ---------- 序列化 ----------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TranscriptionResult":
        """反序列化（服务化场景：RemoteASRBackend 的 HTTP 响应）。"""
        prov = data.get("provenance") or {}
        segs = []
        for s in data.get("segments") or []:
            words = [WordInfo(**w) for w in (s.get("words") or [])]
            segs.append(Seg(
                start=float(s.get("start", 0.0)), end=float(s.get("end", 0.0)),
                text=s.get("text", ""), words=words,
                speaker=s.get("speaker"), emotion=s.get("emotion"),
                events=list(s.get("events") or []),
                avg_logprob=float(s.get("avg_logprob", 0.0)),
                compression_ratio=float(s.get("compression_ratio", 0.0)),
                no_speech_prob=float(s.get("no_speech_prob", 0.0))))
        return cls(
            language=data.get("language", ""),
            duration=float(data.get("duration", 0.0)),
            duration_after_vad=(float(data["duration_after_vad"])
                                if data.get("duration_after_vad") is not None else None),
            segments=segs,
            provenance=Provenance(**{
                k: v for k, v in prov.items()
                if k in Provenance.__dataclass_fields__}),
        )

    @classmethod
    def from_api(cls, payload: dict) -> "TranscriptionResult":
        """兼容厂商 JSON：把 {text, segments:[{start,end,text,words:[...]}]} 归一化。

        对齐 §3.1 的服务化接入——引擎无关，只要能吐这个形状就能接。
        """
        segs: list[Seg] = []
        for i, s in enumerate(payload.get("segments") or []):
            words = []
            for w in s.get("words") or []:
                # 兼容 word/text 两种字段名，以及 start/end 缺失时回退段边界
                words.append(WordInfo(
                    text=w.get("word") or w.get("text") or "",
                    start=float(w.get("start", s.get("start", 0.0))),
                    end=float(w.get("end", s.get("end", 0.0))),
                    prob=float(w.get("probability", w.get("prob", 1.0))),
                    speaker=w.get("speaker")))
            segs.append(Seg(
                start=float(s.get("start", 0.0)), end=float(s.get("end", 0.0)),
                text=s.get("text", ""), words=words,
                speaker=s.get("speaker"), emotion=s.get("emotion"),
                events=list(s.get("events") or []),
                avg_logprob=float(s.get("avg_logprob", 0.0)),
                compression_ratio=float(s.get("compression_ratio", 0.0)),
                no_speech_prob=float(s.get("no_speech_prob", 0.0))))
        prov = payload.get("provenance") or {}
        return cls(
            language=payload.get("language", prov.get("language", "")),
            duration=float(payload.get("duration", 0.0)),
            duration_after_vad=(float(payload["duration_after_vad"])
                                if payload.get("duration_after_vad") is not None else None),
            segments=segs,
            provenance=Provenance(**{
                k: v for k, v in prov.items()
                if k in Provenance.__dataclass_fields__}),
        )


# ── 实时链路契约 ──────────────────────────────────────────────────────

@dataclass
class AsrDelta:
    """流式转写增量：partial 可被推翻，final 才可入史（§1.5 铁律）。"""
    text: str                 # 本次增量文本
    is_final: bool
    end: float = 0.0          # 相对会话起点的秒数
    confirmed: str = ""       # LocalAgreement 已确认的累计文本（final 时等于 text）


@dataclass
class TtsChunk:
    """流式合成音频块：seq 递增无洞，播放器据此检测丢块。"""
    seq: int
    audio: bytes
    is_final: bool = False
    text: str = ""            # 该块对应的文本（便于前端高亮 / 断点截断）
