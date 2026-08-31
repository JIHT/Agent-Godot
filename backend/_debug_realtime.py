import asyncio
import traceback

import numpy as np

from agent_godot.voice.config import RealtimeConfig
from agent_godot.voice.realtime import RealtimeSession
from agent_godot.voice.schema import AsrDelta, TtsChunk

SR = 16000


def _frame(v):
    rng = np.random.default_rng(1)
    n = int(SR * 20 / 1000)
    return (rng.normal(0, 0.4 if v else 0.0005, n)).astype("<i2").tobytes()


class FakeASR:
    async def feed(self, pcm):
        return
        yield

    async def finish(self):
        yield AsrDelta(text="hi", is_final=True, confirmed="hi")

    async def aclose(self):
        pass


class Synth:
    async def synthesize_stream(self, sentences, **kw):
        texts = []
        if hasattr(sentences, "__aiter__"):
            async for s in sentences:
                texts.append(s)
        else:
            texts = list(sentences)
        print("SYNTH texts:", texts)
        for i in range(12):
            await asyncio.sleep(0)
            yield TtsChunk(seq=i, audio=b"\x00\x00" * 100, text=str(i))
        yield TtsChunk(seq=12, audio=b"", is_final=True)

    async def aclose(self):
        pass


async def llm(t):
    for p in ("a,", "b."):
        yield p


chunks = []


def sink(c):
    chunks.append(c)


async def mic(frames):
    for f in frames:
        yield f
        await asyncio.sleep(0)


class Debug(RealtimeSession):
    async def _endpoint(self, **kw):
        r = await super()._endpoint(**kw)
        if kw.get("silence_ms", 0) > 0:
            print(f"  endpoint? silence={kw['silence_ms']:.0f} "
                  f"utt={kw['utterance_ms']:.0f} partial={kw['partial_changed']} -> {r}")
        return r


async def main():
    s = Debug(asr=FakeASR(), tts=Synth(), llm_stream=llm, sink=sink,
              cfg=RealtimeConfig(), frame_ms=20)
    try:
        await s.run(mic([_frame(True)] * 20 + [_frame(False)] * 30))
    except Exception:
        traceback.print_exc()
    print("state:", s.state)
    print("chunks:", len(chunks))
    print("history:", s.history)
    print("ttfa:", s.ttfa_ms)
    print("endpoint_calls: silence/utterance reset ok")


asyncio.run(main())
