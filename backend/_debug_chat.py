import asyncio
import os
import sys
import traceback

os.environ["VOICE_ASR_DEFAULT"] = "mock"
os.environ["VOICE_TTS_DEFAULT"] = "mock"
os.environ["VOICE_ALIGN_ENABLED"] = "false"

import numpy as np

import agent_godot.core as core


class _Resp:
    content = "总评 7 分。语速正常。最严重的问题：填充词偏多，需要刻意减少。"


class _LLM:
    async def complete(self, req):
        print("  [llm called]", flush=True)
        return _Resp()


class _Reg:
    def llm_for_mode(self, mode):
        return _LLM()


core.load_registry = lambda: _Reg()

from pathlib import Path

from agent_godot.cli import run_voice_chat

# 生成合成音频
import wave

SR = 16000
rng = np.random.default_rng(42)
n = 25 * SR
x = rng.normal(0, 0.003, n).astype(np.float32)
t = np.arange(n) / SR
for s, e in [(3.0, 12.0), (18.0, 25.0)]:
    a, b = int(s * SR), int(e * SR)
    env = 0.25 * (0.6 + 0.4 * np.sin(2 * np.pi * 3.0 * t[a:b]))
    x[a:b] = (env * rng.normal(0, 1.0, b - a)).astype(np.float32)
wav = Path("_tmp_chat.wav")
pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
with wave.open(str(wav), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())


async def main():
    try:
        await asyncio.wait_for(run_voice_chat(str(wav), "_tmp_reply.wav", "zh"),
                               timeout=25)
        print("OK: run_voice_chat returned", flush=True)
    except asyncio.TimeoutError:
        print("TIMEOUT: run_voice_chat 卡住", flush=True)
        for task in asyncio.all_tasks():
            print("---- pending task:", task, flush=True)
            task.print_stack(limit=8)
    except Exception:
        traceback.print_exc()


asyncio.run(main())
print("script done", flush=True)
sys.exit(0)
