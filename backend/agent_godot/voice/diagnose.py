"""voice/diagnose.py —— 诊断报告生成（M16 §1.3）

形态：**体检报告** = 化验单数值（features）+ 医生解读（LLM）。
解读必须指着化验单说话（"谷丙转氨酶 62，参考区间 9-50，偏高"），
不许空口"肝功能欠佳"。

两条硬纪律（§1.3 ⑤）：
1. **实测数据优先采信，不得改写数值**——LLM 若改了数就是幻觉实锤，
   可用一条正则自动比对（这是"LLM 幻觉的检测接口"）。
2. **必须指出最严重的一个问题**——不写这条，LLM 会和稀泥。

长转写（> max_transcript_chars）超上下文 → **分段诊断再汇总**
（复用 M07 压缩思想）。
"""
from __future__ import annotations

import json
import logging
import re

from .config import DiagnoseConfig
from .features import SpeechFeatures, to_diagnosis_input
from .schema import TranscriptionResult

logger = logging.getLogger(__name__)

__all__ = ["DIAGNOSE_PROMPT", "diagnose", "verify_no_number_drift"]

DIAGNOSE_PROMPT = """基于实测数据与转写，生成口语诊断报告。

实测数据（**优先采信，不得改写任何数值**）：
{features_json}

转写全文（用于结构与内容分析）：
{transcript}

输出格式：
1. 总评（10 分制，先给分数再给一句话理由）
2. 流利度（实测值对照参考区间，明确"正常/偏快/偏慢"）
3. 填充词 Top3（给出出现的具体语境）
4. 结构建议（STAR/要点覆盖是否完整）
5. 三条改进项（可操作，不要空话）

要求：
- 每个论断必须标注数据依据，格式 [实测: X, 参考: Y]
- **必须明确指出最严重的一个问题**，不许和稀泥
- 数据与转写内容矛盾时以数据为准
- 报告中出现的数值必须与上面的实测数据完全一致"""

_CHUNK_PROMPT = """以下是长录音的第 {i}/{n} 段转写，提取该段的：
1. 核心要点（3 条以内）
2. 该段明显的表述问题（如有）
3. 该段情感倾向（一句话）

转写：
{chunk}"""

_AGGREGATE_PROMPT = """以下是长录音分段诊断的结果，请汇总成一份完整报告。

实测数据（**优先采信，不得改写数值**）：
{features_json}

分段要点：
{chunks_json}

按此格式输出：总评(10分制)/流利度(对照参考区间)/填充词Top3(语境)/结构建议/3条改进项。
要求：每个论断标注数据依据 [实测: X, 参考: Y]；必须指出最严重的一个问题。"""


async def diagnose(llm, tr: TranscriptionResult, features: SpeechFeatures,
                   cfg: DiagnoseConfig | None = None,
                   *, model: str = "voice-diagnose") -> str:
    """生成诊断报告。llm 是 M02 的 LLM 适配器（有 .complete 方法）。

    `model` 只是请求元数据（M02 适配器会用 `_resolved_model` 覆盖它），
    但 LLMRequest 的 model 是必填位置参数，必须给。
    """
    from ..core import LLMRequest, Message
    from .config import DiagnoseConfig as _DC
    from .features import to_diagnosis_input as _tdi
    from .normalize import redact_result
    cfg = cfg or _DC()

    features_json = json.dumps(_tdi(features, tr), ensure_ascii=False, indent=2)
    # ★ 送 LLM 前脱敏：别把手机号/密钥喂给第三方模型（§9.2 缺口 15）
    safe = redact_result(tr)
    transcript = safe.text

    if len(transcript) <= cfg.max_transcript_chars:
        prompt = DIAGNOSE_PROMPT.format(features_json=features_json,
                                        transcript=transcript)
    else:
        prompt = await _long_form(llm, transcript, features_json, cfg, model)

    resp = await llm.complete(LLMRequest(
        model=model, stream=False,
        messages=[Message(role="user", content=prompt)]))
    return (resp.content or "").strip()


async def _long_form(llm, transcript: str, features_json: str,
                     cfg: DiagnoseConfig, model: str = "voice-diagnose") -> str:
    """长转写：分段提取要点 → 汇总（map-reduce，防超上下文）。"""
    from ..core import LLMRequest, Message

    chunks = _split_by_sentence(transcript, cfg.max_transcript_chars)
    parts: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        resp = await llm.complete(LLMRequest(model=model, stream=False,
            messages=[Message(
            role="user",
            content=_CHUNK_PROMPT.format(i=i, n=len(chunks), chunk=chunk))]))
        parts.append(f"[第 {i}/{len(chunks)} 段]\n{(resp.content or '').strip()}")

    return _AGGREGATE_PROMPT.format(
        features_json=features_json,
        chunks_json=json.dumps(parts, ensure_ascii=False, indent=2))


def _split_by_sentence(text: str, limit: int) -> list[str]:
    """按句末标点切块，每块尽量接近但不超过 limit。"""
    parts = re.split(r"(?<=[。！？!?；;\n])", text)
    out, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) > limit:
            out.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        out.append(cur)
    return out or [text]


# ── 幻觉检测接口 ──────────────────────────────────────────────────────

def verify_no_number_drift(report: str, features: SpeechFeatures) -> list[str]:
    """校验报告里的数值是否与特征一致——**LLM 改数即幻觉实锤**（§1.3 ⑤）。

    返回不一致的字段名列表；空列表表示通过。
    正则是"检测接口"而非"修复器"：发现了报警，不自动改。
    """
    issues: list[str] = []
    checks = {
        "语速": features.speech_rate,
        "填充词次数": float(features.filler_count),
        "停顿次数": float(len(features.pauses)),
    }
    for name, value in checks.items():
        # 报告里应出现该数值（int 或 float 形态都接受）
        for variant in (f"{value:g}", f"{value:.1f}", f"{value:.0f}"):
            if variant in report:
                break
        else:
            issues.append(name)
    return issues
