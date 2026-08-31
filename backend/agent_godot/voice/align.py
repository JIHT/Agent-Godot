"""voice/align.py —— wav2vec2 CTC 强制对齐（M16 §9.2 缺口 2）

为什么需要：whisper 的词级时间戳是**注意力权重的副产品**（DTW 从 cross-attention
反推），而 CTC 强制对齐是**专门任务**。§1.2 的全部流利度指标都是时间戳的函数，
时间戳精度直接决定诊断可信度（验收要求：词边界偏差中位数 < 120ms）。

语言处理（对齐 WhisperX 的做法）：
- 空格语言（en/fr/...）：按空格切词，空格转 `|`（wav2vec2 约定）
- **非空格语言（zh/ja）：逐字符对齐**（每个字符当作一个 word）

★ 铁律（§9.4.2）：强制对齐必须在**时间戳已还原到原始音频轴之后**做。
  若先对齐再还原、或还原漏了 pad，CTC 会对着错误的区间硬对齐——
  **宁可不对齐，也不要错对齐**。所以本模块任何异常都回退到原始时间戳。

重依赖（torch / torchaudio / transformers）全部函数内懒导入；未安装时
`align_words` 原样返回，仅把 `provenance.aligned` 置 False。
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import AlignConfig
from .schema import Seg, TranscriptionResult, WordInfo

logger = logging.getLogger(__name__)

__all__ = ["align_words", "AlignUnavailable"]

# torchaudio 自带 bundle 的语言；其余走 HuggingFace
_TORCHAUDIO_LANGS = {"en": "WAV2VEC2_ASR_BASE_960H",
                     "fr": "VOXPOPULI_ASR_BASE_10K_FR",
                     "de": "VOXPOPULI_ASR_BASE_10K_DE",
                     "es": "VOXPOPULI_ASR_BASE_10K_ES",
                     "it": "VOXPOPULI_ASR_BASE_10K_IT"}
# HF 上现成的中文/日文音素对齐模型
_HF_LANGS = {
    "zh": "jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn",
    "ja": "jonatasgrosman/wav2vec2-large-xlsr-53-japanese",
}
_NON_SPACE_LANGS = {"zh", "ja", "yue"}


class AlignUnavailable(RuntimeError):
    """对齐依赖缺失或对齐失败。调用方应回退到原始时间戳。"""


def align_words(tr: TranscriptionResult, wav: Path, lang: str,
                cfg: AlignConfig | None = None) -> TranscriptionResult:
    """把词级时间戳精修到字符级精度。失败一律回退，**绝不抛出**。"""
    from .config import AlignConfig as _AC
    cfg = cfg or _AC()
    if not cfg.enabled:
        return tr

    try:
        return _align(tr, wav, lang or tr.language or "en", cfg)
    except Exception as e:                             # noqa: BLE001
        if not cfg.fallback_to_asr:
            raise
        logger.warning("强制对齐不可用，回退原始时间戳: %s", e)
        tr.provenance.aligned = False
        return tr


# ── 核心 ──────────────────────────────────────────────────────────────

def _align(tr: TranscriptionResult, wav: Path, lang: str,
           cfg: AlignConfig) -> TranscriptionResult:
    import torch
    import torchaudio

    processor, model, device = _load_align_model(lang)
    sr = int(getattr(processor, "sampling_rate", 16000))

    import numpy as np
    from .preprocess import load_audio, resample
    audio, orig_sr = load_audio(wav, target_sr=sr)
    if orig_sr != sr:
        audio = resample(audio, orig_sr, sr)

    waveform = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).unsqueeze(0)
    waveform = waveform.to(device)

    for seg in tr.segments:
        text = _normalize_text(seg.text, lang)
        if not text:
            continue
        a, b = max(0.0, seg.start - 0.25), seg.end + 0.25     # 两侧留余量
        chunk = waveform[:, int(a * sr): int(b * sr)]
        if chunk.numel() < 400:                                # wav2vec2 最短输入
            continue
        with torch.no_grad():
            emission = torch.log_softmax(model(chunk).logits, dim=-1)[0]
        aligned = _align_segment(emission, processor, text, lang, a, sr, cfg.beam_width)
        if aligned:
            seg.words = aligned
            seg.start, seg.end = aligned[0].start, aligned[-1].end

    tr.provenance.aligned = True
    return tr


def _normalize_text(text: str, lang: str) -> str:
    """对齐前文本归一：去空白、去标点、按需转大写（wav2vec2 词表是大写）。"""
    import re

    t = re.sub(r"\s+", " ", text).strip()
    if lang in _NON_SPACE_LANGS:
        # 中文：只保留 CJK、字母、数字（逐字符对齐）
        return "".join(c for c in t if _keep_zh(c))
    t = "".join(c for c in t if c.isalnum() or c in " '")
    return t.upper()


def _keep_zh(c: str) -> bool:
    o = ord(c)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or c.isalnum())


def _load_align_model(lang: str):
    """加载对齐模型：torchaudio bundle 优先，其次 HuggingFace。"""
    key = (lang or "en").split("-")[0].lower()
    if key in _TORCHAUDIO_LANGS:
        import torchaudio
        bundle = getattr(torchaudio.pipelines, _TORCHAUDIO_LANGS[key])
        return bundle.get_processor(), bundle.get_model(), _device()
    model_id = _HF_LANGS.get(key)
    if model_id is None:
        raise AlignUnavailable(f"语言 {lang!r} 没有可用的对齐模型"
                               f"（支持: {sorted(_TORCHAUDIO_LANGS | _HF_LANGS)}）")
    from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor
    processor = Wav2Vec2Processor.from_pretrained(model_id)
    model = Wav2Vec2ForCTC.from_pretrained(model_id)
    return processor, model.eval(), _device()


def _device():
    import torch
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


# ── CTC 强制对齐算法 ──────────────────────────────────────────────────

def _align_segment(emission, processor, text: str, lang: str, offset: float,
                   sr: int, beam_width: int) -> list[WordInfo]:
    """标准 CTC 强制对齐：trellis 前向 → 回溯 → 合并重复 → 词/字符级时间戳。"""
    import torch

    tokenizer, dictionary = _get_tokenizer(processor)
    tokens = [dictionary.get(c, dictionary.get("|", 0)) for c in tokenizer(text)]
    tokens = [t for t in tokens if t is not None]
    if not tokens:
        return []

    trellis = _get_trellis(emission, tokens)
    path = _backtrack(trellis, emission, tokens)
    spans = _merge_repeats(path, tokens)

    # 词单位：非空格语言逐字符，空格语言按空格切
    if lang in _NON_SPACE_LANGS:
        units = [(i, c) for i, c in enumerate(text)]
    else:
        units = [(i, w) for i, w in enumerate(text.split(" ")) if w]

    ratio = emission.shape[0] / (emission.shape[0] / sr)
    words: list[WordInfo] = []
    idx = 0
    for _, unit_text in units:
        n = len(unit_text)
        chunk_spans = spans[idx: idx + n]
        idx += n
        if not chunk_spans:
            continue
        start = offset + chunk_spans[0][0].item() / sr
        end = offset + (chunk_spans[-1][-1].item() + 1) / sr
        words.append(WordInfo(text=unit_text, start=round(float(start), 3),
                              end=round(float(end), 3)))
    return words


def _get_tokenizer(processor):
    """(分词函数, 字表) —— 屏蔽 torchaudio bundle 与 HF processor 的差异。"""
    if hasattr(processor, "tokenizer"):               # torchaudio bundle
        def tok(t: str) -> list[str]:
            return list(t.replace(" ", "|"))
        return tok, {c: i for i, c in enumerate(processor.get_labels() if hasattr(
            processor, "get_labels") else [])}
    vocab = processor.tokenizer.get_vocab()           # HF processor
    def tok_hf(t: str) -> list[str]:
        return list(t.replace(" ", "|"))
    return tok_hf, vocab


def _get_trellis(emission, tokens):
    """CTC 前向：trellis[t, j] = 在 t 帧处于第 j 个 token 的最大累积得分。"""
    import torch

    num_frames, num_tokens = emission.shape[0], len(tokens)
    trellis = torch.full((num_frames, num_tokens), -float("inf"))
    trellis[0, 0] = emission[0, tokens[0]]
    for t in range(1, num_frames):
        trellis[t, 0] = trellis[t - 1, 0] + emission[t, tokens[0]]
        for j in range(1, num_tokens):
            trellis[t, j] = max(
                trellis[t - 1, j],                    # 停留（发 blank 或重复）
                trellis[t - 1, j - 1],                # 前进
            ) + emission[t, tokens[j]]
    return trellis


def _backtrack(trellis, emission, tokens):
    """回溯最优路径：从得分最高的末帧位置往回走。"""
    import torch

    t, j = trellis.shape[0] - 1, trellis.shape[1] - 1
    path = [(j, t)]
    while j > 0:
        moved = trellis[t - 1, j - 1] >= trellis[t - 1, j] if t > 0 else True
        if moved and j - 1 >= 0:
            j -= 1
        t -= 1
        if t <= 0:
            break
        path.append((j, t))
    return path[::-1]


def _merge_repeats(path, tokens):
    """合并连续相同 token，得到每个 token 的起止帧区间。"""
    spans = []
    prev = None
    for j, t in path:
        if prev is not None and prev[0] == j:
            spans[-1].append(t)
        else:
            spans.append([t])
            prev = (j, t)
    return spans
