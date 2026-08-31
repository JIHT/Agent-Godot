"""SenseVoice 中文引擎测试（M16 §9.2 缺口 1、5）

SenseVoice-Small（234M / Apache 2.0）是中文路径的默认引擎。除了转写，它
还在一个模型里顺带输出**情感**与**音频事件**——这是它相对 whisper 的
第二重优势（第一重是 CER：3.0% vs 5.14%，会议场景 4.53% vs 18.87%）。

本测试全程**不加载模型**（环境无 funasr）：用假的返回结构验证解析逻辑，
用假的模型对象验证参数分流。
"""
from __future__ import annotations

import pytest

from agent_godot.voice.schema import TranscriptionResult
from agent_godot.voice.stt import (ENGINE_FACTORIES, BackendUnavailable,
                                   FunASRBackend, _funasr_engine_name,
                                   _parse_funasr, build_backends)

# ── 输出解析 ──────────────────────────────────────────────────────────

SV_TEXT = "<|HAPPY|>大家好我叫小明<|Speech|><|Applause|>"
SV_TS = [[3000, 3200], [3200, 3450], [3450, 3700], [3700, 3900],
         [3900, 4100], [4100, 4300], [4300, 4600]]


def test_parse_extracts_emotion_and_events():
    """★ 情感与事件以 <|TAG|> 内联在 text 里——必须抽出来。

    不抽的话 LLM 会在诊断报告里读到 "<|HAPPY|>" 这种噪音，而且
    副语言层信息（紧张度/自信度）就白白丢了。
    """
    tr = _parse_funasr([{"text": SV_TEXT, "timestamp": SV_TS}], "sensevoice",
                       engine="iic/SenseVoiceSmall")

    assert tr.segments[0].emotion == "HAPPY"
    assert "Applause" in tr.segments[0].events
    # 正文里不能残留标签
    assert "<|" not in tr.text
    assert tr.text == "大家好我叫小明"


def test_parse_converts_millisecond_timestamps():
    """FunASR 的 timestamp 单位是**毫秒**，且是字级（中文非空格语言）。"""
    tr = _parse_funasr([{"text": SV_TEXT, "timestamp": SV_TS}], "sensevoice",
                       engine="iic/SenseVoiceSmall")

    words = tr.words
    assert len(words) == 7                       # 7 个字
    assert words[0].start == pytest.approx(3.0)  # 3000ms → 3.0s
    assert words[0].end == pytest.approx(3.2)
    assert words[-1].end == pytest.approx(4.6)
    assert "".join(w.text for w in words) == "大家好我叫小明"


def test_parse_duration_left_to_transcriber():
    """★ duration 必须由 Transcriber 从 wav 探得，不能用"最后字结束时间"。

    用 last.end 当 duration 会让 axis_warning() 与 VAD 裁剪率全部失真。
    """
    tr = _parse_funasr([{"text": SV_TEXT, "timestamp": SV_TS}], "sensevoice",
                       engine="iic/SenseVoiceSmall")
    assert tr.duration == 0.0                    # 留给上层填真实时长
    assert tr.words[-1].end > 0                  # 但词时间戳是有的


def test_parse_language_not_hardcoded():
    """粤语/日语/英语也要如实记录——硬编码 zh 会让语速单位全错。"""
    for lang, want in (("yue", "yue"), ("ja", "ja"), (None, "zh")):
        tr = _parse_funasr([{"text": "测试", "timestamp": []}], "sensevoice",
                           engine="iic/SenseVoiceSmall", lang=lang)
        assert tr.language == want


def test_parse_handles_multi_segment_vad_output():
    """★ 挂了 fsmn-vad 后长音频返回**多段**列表。

    只取 res[0] 会静默丢掉后面所有内容——3 分钟录音只剩前 30 秒，而且
    不报错、不告警，是最难发现的一类 bug。
    """
    res = [
        {"text": "<|NEUTRAL|>第一段话<|Speech|>",
         "timestamp": [[1000, 1200], [1200, 1400], [1400, 1600], [1600, 1800]]},
        {"text": "<|HAPPY|>第二段话<|Speech|><|Laughter|>",
         "timestamp": [[9000, 9200], [9200, 9400], [9400, 9600], [9600, 9800]]},
        {"text": "<|SAD|>第三段话<|Speech|>",
         "timestamp": [[20000, 20200], [20200, 20400], [20400, 20600],
                       [20600, 20800]]},
    ]
    tr = _parse_funasr(res, "sensevoice", engine="iic/SenseVoiceSmall")

    assert len(tr.segments) == 3, "多段输出必须全部保留"
    assert tr.text == "第一段话第二段话第三段话"
    assert len(tr.words) == 12
    # 每段各自的时间戳与情感
    assert [s.emotion for s in tr.segments] == ["NEUTRAL", "HAPPY", "SAD"]
    assert tr.segments[0].start == pytest.approx(1.0)
    assert tr.segments[2].start == pytest.approx(20.0)
    assert tr.segments[1].events == ["Laughter"]


def test_parse_tolerates_missing_timestamp():
    """没开 output_timestamp 时不能崩，退化成纯文本。"""
    tr = _parse_funasr([{"text": "你好"}], "sensevoice", engine="iic/SenseVoiceSmall")
    assert tr.text == "你好"
    assert tr.words == []


def test_parse_tolerates_empty_result():
    tr = _parse_funasr([], "sensevoice", engine="iic/SenseVoiceSmall")
    assert isinstance(tr, TranscriptionResult) and not tr.text


# ── 参数分流 ──────────────────────────────────────────────────────────

def test_params_split_model_vs_infer_kwargs():
    """★ 构造参数与推理参数必须分开传——混传会 TypeError 或静默失效。

    这是接 FunASR 最常见的坑：把 output_timestamp 塞进 AutoModel()
    不会报错，只是完全不生效，然后你以为这个模型不出时间戳。
    """
    b = FunASRBackend(
        "iic/SenseVoiceSmall",
        device="cuda:0", vad_model="fsmn-vad", punc_model="ct-punc",
        output_timestamp=True, ban_emo_unk=False, merge_length_s=15)

    assert b.model_kwargs == {"device": "cuda:0", "vad_model": "fsmn-vad",
                              "punc_model": "ct-punc"}
    assert b.infer_kwargs == {"output_timestamp": True, "ban_emo_unk": False,
                              "merge_length_s": 15}


def test_engine_name_and_emotion_capability():
    b = FunASRBackend("iic/SenseVoiceSmall")
    assert b.name == "sensevoice"
    assert b.supports_emotion is True

    b2 = FunASRBackend("FireRedTeam/FireRedASR2-AED")
    assert b2.name == "firered"
    assert b2.supports_emotion is False        # FireRed 不出情感


@pytest.mark.parametrize("model,want", [
    ("iic/SenseVoiceSmall", "sensevoice"),
    ("paraformer-zh", "paraformer"),
    ("FireRedTeam/FireRedASR2-AED", "firered"),
    ("Qwen/Qwen3-ASR-0.6B", "qwen"),
])
def test_funasr_engine_name(model, want):
    assert _funasr_engine_name(model) == want


def test_factory_allows_model_override():
    """配置里的 model 键可覆盖默认模型（换微调版 / 换镜像源）。"""
    make = ENGINE_FACTORIES["sensevoice"]
    assert make().model == "iic/SenseVoiceSmall"
    assert make(model="my/sensevoice-ft", device="cpu").model == "my/sensevoice-ft"


def test_sensevoice_is_default_route():
    """中文默认走 SenseVoice（装了 funasr 才真正可用，但配置应当如此）。"""
    from agent_godot.voice.config import load_voice_config
    cfg = load_voice_config()
    assert cfg.asr.default == "sensevoice"
    assert cfg.asr.routing["zh"] == "sensevoice"
    assert cfg.asr.routing["yue"] == "sensevoice"
    # 英文仍是 whisper 主场
    assert cfg.asr.routing["en"] == "whisper"


def test_sensevoice_config_has_vad_and_punc():
    """两条硬约束必须在配置里体现，否则长音频/无标点会静默劣化。

    · SenseVoice 单次只吃 ≤30s → 必须挂 fsmn-vad
    · SenseVoice 输出不带标点 → 必须挂 ct-punc
    """
    from agent_godot.voice.config import load_voice_config
    eng = load_voice_config().asr.engines["sensevoice"]
    assert eng["vad_model"] == "fsmn-vad"
    assert eng["vad_kwargs"]["max_single_segment_time"] == 30000
    assert eng["punc_model"] == "ct-punc"
    assert eng["output_timestamp"] is True


def test_build_backends_without_funasr_installed():
    """没装 funasr 也能构建后端对象（懒加载）——只有真正调用才报错。"""
    from agent_godot.voice.config import AsrConfig
    backends = build_backends(AsrConfig(default="sensevoice",
                                        routing={"zh": "sensevoice"},
                                        quality_routing={"default": "sensevoice"}))
    assert "sensevoice" in backends
    assert isinstance(backends["sensevoice"], FunASRBackend)


def test_load_raises_actionable_error_without_funasr():
    """缺依赖要给"怎么装"的提示，不能只抛 ModuleNotFoundError。"""
    b = FunASRBackend("iic/SenseVoiceSmall")
    try:
        b._load()
    except BackendUnavailable as e:
        assert "pip install funasr" in str(e)
    except ImportError:
        pytest.fail("应当包装成 BackendUnavailable 并给出安装提示")


# ── 情感进诊断输入 ────────────────────────────────────────────────────

def test_emotion_flows_into_diagnosis_input():
    """情感分布要进 LLM 输入——这是副语言层唯一的量化信号。

    原方案三个维度（流利度/填充词/结构）全在文本层；面试官在意的
    紧张度/自信度恰恰藏在韵律里，文本看不出来。
    """
    from agent_godot.voice.config import FeaturesConfig
    from agent_godot.voice.features import extract_features, to_diagnosis_input

    res = [{"text": "<|SAD|>大家好我叫小明<|Speech|>",
            "timestamp": SV_TS[:4]},
           {"text": "<|HAPPY|>我做过一个项目<|Speech|>",
            "timestamp": [[8000, 8200], [8200, 8400], [8400, 8600],
                          [8600, 8800], [8800, 9000], [9000, 9200],
                          [9200, 9400]]}]
    tr = _parse_funasr(res, "sensevoice", engine="iic/SenseVoiceSmall")
    tr.duration = 10.0

    cfg = FeaturesConfig()
    d = to_diagnosis_input(extract_features(tr, cfg), tr, cfg)
    assert d["情感分布"] == {"SAD": 1, "HAPPY": 1}
