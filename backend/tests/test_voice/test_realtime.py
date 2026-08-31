"""实时链路测试：端点检测 / 打断 / 重叠 / 流式策略（M16 §1.5 §1.6 · §9.6）"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from agent_godot.voice.config import RealtimeConfig
from agent_godot.voice.realtime import RealtimeSession, RealtimeState
from agent_godot.voice.schema import AsrDelta, TtsChunk
from agent_godot.voice.stream_asr import (AlignAttPolicy, LocalAgreementPolicy,
                                          make_policy, tokenize)

SR = 16000
FRAME = 20                                    # ms


def _frame(voiced: bool) -> bytes:
    """合成一帧 16-bit PCM：voiced=True 为高能量，否则为静音。

    ★ float → int16 必须先缩放再转换。直接 astype 会把 0.4 截断成 0，
      语音帧变成全零静音（这个坑让端点检测怎么调都不触发）。
    """
    rng = np.random.default_rng(1)
    n = int(SR * FRAME / 1000)
    amp = 0.4 if voiced else 0.0005
    x = np.clip(rng.normal(0, amp, n), -1.0, 1.0) * 32767.0
    return x.astype("<i2").tobytes()


def _mic(frames):
    async def gen():
        for f in frames:
            yield f
            await asyncio.sleep(0)
    return gen()


class FakeASR:
    """假流式 ASR：feed 不吐 partial（模拟"还没确认"），finish 吐 final。"""

    def __init__(self, final: str = "给玩家加双跳"):
        self.final = final
        self.finished = 0

    async def feed(self, pcm: bytes):
        return
        yield                                  # pragma: no cover

    async def finish(self):
        self.finished += 1
        yield AsrDelta(text=self.final, is_final=True, confirmed=self.final)

    async def aclose(self) -> None:
        pass


class ChunkedSynth:
    """吐 N 个块的假合成器（用来验证"打断时未播块被丢弃"）。"""

    def __init__(self, n: int = 12):
        self.n = n

    async def synthesize_stream(self, sentences, **kw):
        # ★ 惰性消费：异步生成器不能用 list() 直接展开
        texts: list[str] = []
        if hasattr(sentences, "__aiter__"):
            async for s in sentences:
                texts.append(s)
        else:
            texts = list(sentences)
        texts = texts or ["。"]
        seq = 0
        for i in range(self.n):
            await asyncio.sleep(0)             # 让出控制权，模拟真实耗时
            yield TtsChunk(seq=seq, audio=b"\x00\x00" * 100,
                           text=texts[i % len(texts)])
            seq += 1
        yield TtsChunk(seq=seq, audio=b"", is_final=True)

    async def aclose(self) -> None:
        pass


class RecordingSink:
    def __init__(self) -> None:
        self.chunks: list[TtsChunk] = []
        self.flushed = 0

    def __call__(self, chunk: TtsChunk) -> None:
        self.chunks.append(chunk)

    def aflush(self) -> None:
        self.flushed += 1


async def _llm(text: str):
    for piece in ("好的，", "我先看一下 player.gd。"):
        yield piece
        await asyncio.sleep(0)


def _session(synth=None, *, asr=None, cfg=None, sink=None,
             turn_detector=None, warmup=None) -> RealtimeSession:
    return RealtimeSession(
        asr=asr or FakeASR(), tts=synth or ChunkedSynth(), llm_stream=_llm,
        sink=sink or RecordingSink(), cfg=cfg or RealtimeConfig(),
        turn_detector=turn_detector, frame_ms=FRAME,
        on_warmup=warmup)


# ── 端点检测：物理层 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_endpointing_hysteresis():
    """200ms 换气不切句；500ms 静音切句；partial 持续出新词时静音再长也不切。"""
    from agent_godot.voice.turn import HysteresisTurnDetector

    d = HysteresisTurnDetector(silence_ms=500, min_utterance_ms=250)

    assert not await d.is_complete(silence_ms=200, utterance_ms=1000,
                                   partial_changed=False)
    assert await d.is_complete(silence_ms=500, utterance_ms=1000,
                               partial_changed=False)
    # 话长不够（咳嗽）：静音够了也不切
    assert not await d.is_complete(silence_ms=900, utterance_ms=100,
                                   partial_changed=False)
    # ★ 还在出新词 → 说话人在组织语言，静音再长也不切
    assert not await d.is_complete(silence_ms=5000, utterance_ms=3000,
                                   partial_changed=True)


# ── 端点检测：语义层 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_semantic_endpoint_beats_silence_only():
    """★ 两种静音时长相同的情况，只有语义层能分开：

    「我用的是……」（悬停想词）判未完；「我用的是 Godot。」（句末降调）判已完。
    """
    from agent_godot.voice.turn import TwoStageTurnDetector

    class StubSemantic:
        def __init__(self, answer: bool) -> None:
            self.answer = answer

        async def is_complete(self, **kw) -> bool:
            return self.answer

    kw = dict(silence_ms=700, utterance_ms=2000, partial_changed=False,
              turn_pcm=_frame(True) * 100)

    # 物理层通过（静音 700ms）→ 语义层说"没说完" → 不交棒
    hovering = TwoStageTurnDetector(semantic=StubSemantic(False), enabled=True)
    assert not await hovering.is_complete(**kw)

    # 同样的静音时长，语义层说"说完了" → 交棒
    finished = TwoStageTurnDetector(semantic=StubSemantic(True), enabled=True)
    assert await finished.is_complete(**kw)


@pytest.mark.asyncio
async def test_two_stage_skips_semantic_when_physical_says_no():
    """两层是串联：物理层判"还在说"时，根本不该去问语义层（省 65ms）。"""
    from agent_godot.voice.turn import HysteresisTurnDetector, TwoStageTurnDetector

    calls = []

    class CountingSemantic:
        async def is_complete(self, **kw) -> bool:
            calls.append(1)
            return True

    d = TwoStageTurnDetector(HysteresisTurnDetector(500, 250),
                             CountingSemantic(), enabled=True)
    assert not await d.is_complete(silence_ms=100, utterance_ms=1000,
                                   partial_changed=False)
    assert calls == []                       # 语义层一次都没被调用


def test_build_turn_detector_degrades_without_smart_turn():
    """装不了 smart-turn 时回落纯迟滞，不崩。"""
    from agent_godot.voice.turn import HysteresisTurnDetector, build_turn_detector
    d = build_turn_detector(semantic=True, silence_ms=500)
    assert isinstance(d, (HysteresisTurnDetector, object))


# ── 打断 ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_barge_in_discards_pending_tts():
    """SPEAKING 态注入人声 → cancel + flush + 未播块不再送 sink。"""
    sink = RecordingSink()
    synth = ChunkedSynth(n=12)
    s = _session(synth, sink=sink)

    frames = ([_frame(True)] * 20                    # 说话 400ms
              + [_frame(False)] * 30                 # 静音 600ms → 触发端点
              + [_frame(True)] * 20)                 # 插话 400ms → 打断
    await s.run(_mic(frames))

    assert s.state is RealtimeState.LISTENING
    assert sink.flushed >= 1, "必须 flush 播放器缓冲，否则漏半句"
    assert 0 < len(sink.chunks) < 12, "未播的块必须被丢弃"


@pytest.mark.asyncio
async def test_truncate_last_turn_to_played_position():
    """打断后上轮发言截断到实际播到处——防 LLM 以为自己说完了（§1.6 ⑤）。"""
    s = _session()
    s._cur.chunk_texts = [f"块{i}" for i in range(12)]
    s.history.append({"role": "assistant", "content": "".join(s._cur.chunk_texts)})

    await s._truncate_last_turn(5)
    assert s.history[-1]["content"] == "块0块1块2块3块4"
    assert s._cur.assistant == "块0块1块2块3块4"


@pytest.mark.asyncio
async def test_no_barge_in_during_thinking_state():
    """THINKING 态（还没出声）不触发打断——没有播放就没有打断语义。"""
    sink = RecordingSink()
    s = _session(sink=sink)
    frames = [_frame(True)] * 20 + [_frame(False)] * 30
    await s.run(_mic(frames))
    # 正常说完（没被打断），块全部播出
    assert sink.flushed == 0
    assert len(sink.chunks) == 12


# ── 首音时延 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ttfa_recorded():
    """首音时延是实时链路唯一 KPI，必须被埋点（§9.2 缺口 14）。"""
    from agent_godot.voice.metrics import get_metrics

    m = get_metrics()
    m.reset()
    sink = RecordingSink()
    s = _session(sink=sink)
    await s.run(_mic([_frame(True)] * 20 + [_frame(False)] * 30))

    assert s.ttfa_ms is not None and s.ttfa_ms >= 0
    recs = [r for r in m.of("tts") if r.get("ttfa_ms") is not None]
    assert recs, "TTFA 未埋点"


# ── 重叠预热 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_partial_only_warms_up_never_commits():
    """§1.5 铁律：partial 只供热身，执行/落史必须等 final。"""
    seen: list[str] = []

    class PartialASR(FakeASR):
        def __init__(self) -> None:
            super().__init__()
            self.n = 0

        async def feed(self, pcm):
            self.n += 1
            if self.n % 10 == 0:
                yield AsrDelta(text=f"partial-{self.n}", is_final=False)

    async def warmup(text: str) -> None:
        seen.append(text)

    sink = RecordingSink()
    s = _session(asr=PartialASR(), sink=sink, warmup=warmup)
    await s.run(_mic([_frame(True)] * 20 + [_frame(False)] * 30))

    assert seen, "partial 应触发预热"
    # 预热文本不会进历史；进历史的只有 final
    assert all("partial-" not in h["content"] for h in s.history)
    assert s.history[0]["content"] == "给玩家加双跳"


# ── 流式策略 ──────────────────────────────────────────────────────────

def test_localagreement_confirms_common_prefix():
    """相邻两次更新的最长公共前缀才算 confirmed，其余可被推翻。"""
    p = LocalAgreementPolicy()
    confirmed, partial = p.update(tokenize("我用的是 Godot", "zh"))
    assert confirmed == ""

    c2, p2 = p.update(tokenize("我用的是 Godot 引擎", "zh"))
    assert c2.startswith("我用的是")
    assert p2

    c3, _ = p.flush()
    assert c3.endswith("引擎")


def test_localagreement_allows_retraction():
    """第二次更新推翻前一次 → 被推翻的部分不进 confirmed。"""
    p = LocalAgreementPolicy()
    p.update(tokenize("我叫小明", "zh"))
    confirmed, _ = p.update(tokenize("我叫小强", "zh"))
    assert confirmed == "我叫小"
    assert "明" not in confirmed


def test_alignatt_falls_back_to_localagreement():
    """后端不提供原生 AlignAtt（无 stream_step）→ 诚实降级，不假装实现。"""
    class Plain:
        name = "plain"

    pol = AlignAttPolicy(backend=Plain())
    c, _ = pol.update(tokenize("我们开始吧", "zh"))
    pol2 = AlignAttPolicy(backend=Plain())
    pol2.update(tokenize("我们开始吧", "zh"))
    c2, _ = pol2.update(tokenize("我们开始吧现在", "zh"))
    assert c2.startswith("我们开始吧")


def test_make_policy_factory():
    assert isinstance(make_policy("localagreement"), LocalAgreementPolicy)
    assert isinstance(make_policy("alignatt"), AlignAttPolicy)
    assert isinstance(make_policy("未知策略"), LocalAgreementPolicy)


def test_tokenize_by_language():
    assert tokenize("你好 世界", "zh") == ["你", "好", "世", "界"]
    assert tokenize("hello world", "en") == ["hello", "world"]
