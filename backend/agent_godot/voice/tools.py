"""voice/tools.py —— 语音工具注册（M16 + M04）

两个工具，双身份（§1.3）：
- **transcribe 是工具**：模型可以在对话中调用（转录音 → 文字）
- **语音入口是产品功能**：用户直接说/传录音（M19 API / M20 录音组件）

两个都是 readonly + risk=low：只读音频、不写工作区、不碰 Godot 工程。
（后续若要加"按语音指令改代码"，那走的是 M04 已有的写工具，不是这里。）

★ 隐私纪律（§9.2 缺口 15）：诊断报告送去 LLM 之前先 `redact_result()` 脱敏，
  别把用户录音里的手机号/密钥喂给第三方模型。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from ..tools.registry import BaseTool, register_tool
from ..tools.response import Artifact, ErrorKind, ToolError, ToolResponse

logger = logging.getLogger(__name__)

__all__ = ["TranscribeTool", "AnalyzeSpeechTool", "build_voice_tools"]


def _transcriber():
    """懒构建（工具是无参实例化的，不能要求调用方传 Transcriber）。"""
    from .config import load_voice_config
    from .stt import Transcriber
    return Transcriber(load_voice_config())


@register_tool(name="transcribe", readonly=True, risk="low", tags={"voice"})
class TranscribeTool(BaseTool):
    """把音频文件转写成带时间戳的文本，可选导出字幕。

    支持中文/英文自动检测。返回转写全文、词级时间戳、语言与置信度。
    适合：会议记录、录音整理、语音笔记、字幕生成。
    """
    class Params(BaseModel):
        path: str = Field(description="音频文件路径（wav/mp3/m4a 等）")
        lang: str = Field(default="", description="语言代码如 zh/en；留空自动检测")
        high_accuracy: bool = Field(
            default=False, description="true 走高精度引擎（更慢，用于正式报告）")
        fmt: str = Field(default="text",
                         description="输出格式：text / srt / vtt / json")

    async def run(self, path: str = "", lang: str = "",
                  high_accuracy: bool = False, fmt: str = "text") -> ToolResponse:
        from .export import to_json, to_srt, to_vtt
        from .normalize import redact_result

        p = Path(path)
        if not p.exists():
            return ToolResponse(ok=False, error=ToolError(
                ErrorKind.NOT_FOUND, "transcribe", f"音频文件不存在: {path}",
                hint="确认路径正确；相对路径以项目根为准"))
        try:
            tr = await _transcriber().transcribe(
                p, lang=lang or None, high_accuracy=high_accuracy)
            tr.assert_sane()
        except AssertionError as e:
            # 时间戳不可信也要给文本——但必须明确告知（§9.2 置信度纪律）
            logger.error("时间戳自检失败: %s", e)
            return ToolResponse(ok=False, error=ToolError(
                ErrorKind.INTERNAL, "transcribe",
                f"转写完成但时间戳不可信：{e}",
                hint="多半是 VAD 偏移未还原（§1.1 ⑤a），检查后端是否正确映射原始轴"))
        except Exception as e:                     # noqa: BLE001
            return ToolResponse(ok=False, error=ToolError(
                ErrorKind.INTERNAL, "transcribe", f"转写失败: {e}",
                hint="检查 ASR 引擎是否已安装并在 config/voice.yaml 的 routing 中配置"))

        safe = redact_result(tr)
        if fmt == "srt":
            return ToolResponse(ok=True, summary=to_srt(safe),
                                data={"fmt": "srt"})
        if fmt == "vtt":
            return ToolResponse(ok=True, summary=to_vtt(safe),
                                data={"fmt": "vtt"})
        if fmt == "json":
            return ToolResponse(ok=True, summary=to_json(safe),
                                data={"fmt": "json"})

        words = tr.words
        summary = (
            f"转写完成 · 引擎 {tr.provenance.engine} · 语言 {tr.language}"
            f"（置信度 {tr.provenance.language_prob:.2f}）\n"
            f"时长 {tr.duration:.1f}s · 词/字数 {len(words)} · "
            f"段数 {len(tr.segments)}\n"
            f"{'⚠ 含低置信片段，数值仅供参考' if tr.low_confidence else ''}\n\n"
            f"{safe.text}")
        return ToolResponse(
            ok=True, summary=summary.strip(),
            data={"language": tr.language, "duration": tr.duration,
                  "engine": tr.provenance.engine,
                  "low_confidence": tr.low_confidence,
                  "text": safe.text,
                  "words": [{"text": w.text, "start": w.start, "end": w.end}
                            for w in words]},
            artifacts=[Artifact(type="log", ref=str(p))])


@register_tool(name="analyze_speech", readonly=True, risk="low", tags={"voice"})
class AnalyzeSpeechTool(BaseTool):
    """分析录音的口语表达质量，产出带实测数值的诊断报告。

    指标全部从词级时间戳计算（零额外模型）：语速（字/分 或 词/分，按语言自动
    选单位）、停顿分布（思考型/卡壳型）、填充词密度、节奏方差。报告由 LLM
    结合实测数据生成，每个论断都标注数据依据。
    适合：面试练习、演讲复盘、旁白/台词质检。
    """
    class Params(BaseModel):
        path: str = Field(description="音频文件路径")
        lang: str = Field(default="", description="语言代码如 zh/en；留空自动检测")
        with_report: bool = Field(default=True, description="是否调用 LLM 生成诊断报告")

    async def run(self, path: str = "", lang: str = "",
                  with_report: bool = True) -> ToolResponse:
        from .config import load_voice_config
        from .diagnose import diagnose, verify_no_number_drift
        from .export import to_json
        from .features import extract_features, to_diagnosis_input

        p = Path(path)
        if not p.exists():
            return ToolResponse(ok=False, error=ToolError(
                ErrorKind.NOT_FOUND, "analyze_speech", f"音频文件不存在: {path}",
                hint="确认路径正确"))

        cfg = load_voice_config()
        try:
            tr = await _transcriber().transcribe(p, lang=lang or None,
                                                 high_accuracy=True)
            tr.assert_sane()
        except AssertionError as e:
            logger.error("时间戳自检失败，特征仅供参考: %s", e)
        except Exception as e:                     # noqa: BLE001
            return ToolResponse(ok=False, error=ToolError(
                ErrorKind.INTERNAL, "analyze_speech", f"转写失败: {e}",
                hint="检查 ASR 引擎配置"))

        # ★ 特征在**脱敏之前**算（脱敏会改变文本长度 → 污染语速与填充词）
        feats = extract_features(tr, cfg.features)
        diag_input = to_diagnosis_input(feats, tr, cfg.features)

        report = ""
        if with_report and cfg.diagnose.enabled:
            try:
                from ..core import load_registry
                llm = load_registry().llm_for_mode("ask")
                report = await diagnose(llm, tr, feats, cfg.diagnose)
                drift = verify_no_number_drift(report, feats)
                if drift:
                    # 幻觉检测接口：LLM 改了实测值 → 报警（不自动改，§1.3 ⑤）
                    logger.warning("诊断报告数值与实测不符: %s", drift)
                    report += f"\n\n⚠ 报告数值与实测不一致的项：{drift}"
            except Exception as e:                 # noqa: BLE001
                logger.warning("LLM 诊断失败，仅返回实测特征: %s", e)
                report = "（LLM 诊断不可用，以下为实测数据）"

        summary = (
            f"口语诊断 · 语速 {feats.speech_rate} {feats.rate_unit}"
            f"（{feats.rate_verdict}）· 停顿 {len(feats.pauses)} 次"
            f"（最长 {feats.longest_pause.duration if feats.longest_pause else 0}s）"
            f" · 填充词 {feats.filler_count} 次（{feats.filler_verdict}）"
            f" · 节奏方差 {feats.rhythm_variance}")
        if tr.low_confidence:
            summary += "\n⚠ 转写含低置信片段，数值仅供参考"

        return ToolResponse(
            ok=True,
            summary=(summary + "\n\n" + report).strip() if report else summary,
            data={"features": json.loads(to_json(tr, feats, redact=True)),
                  "diagnosis_input": diag_input,
                  "report": report},
            artifacts=[Artifact(type="log", ref=str(p))])


def build_voice_tools() -> list[BaseTool]:
    """供 M04 装配（与 builtin 六件套同款工厂形态）。"""
    return [TranscribeTool(), AnalyzeSpeechTool()]
