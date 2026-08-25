"""tools/response.py —— 工具结果统一协议（M04 §1.3 完整版）

标准化的工作报告单：老板（模型）只要一页纸——
- ok / summary   → 模型读的 Observation（人话结论）
- data           → 程序消费的结构化数据
- artifacts      → 前端渲染物（diff 卡片/文件引用）
- error(kind+hint) → 失败原因 + 可采取的行动（模型真的会照 hint 做）

铁律：工具永远不向 Loop 抛异常——错误也是数据（ReAct 纠错的精髓）。
summary 渲染时截断兜底（2000 字符），防大结果撑爆上下文（M07 前的保险）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class ErrorKind(Enum):
    """失败原因枚举：可统计（M21 指标 tool_calls_total{status}）、可路由处理。"""
    NOT_FOUND = "not_found"        # 路径/资源不存在
    VALIDATION = "validation"      # 参数校验失败
    TIMEOUT = "timeout"            # 执行超时
    CONFLICT = "conflict"          # 乐观锁版本冲突
    DENIED = "denied"              # 沙箱/权限拒绝
    INTERNAL = "internal"          # 内部异常（兜底）


@dataclass
class ToolError:
    """结构化错误：message 说发生了什么，hint 说模型下一步该做什么。"""
    kind: ErrorKind
    tool: str
    message: str
    hint: str | None = None


@dataclass
class Artifact:
    """前端渲染物：summary 省 token，结构化数据走这里。"""
    type: Literal["diff", "file", "image", "log"]
    ref: str                 # 引用（diff 内容 id / 文件路径 / 截图 URL）
    meta: dict | None = None


@dataclass
class ToolResponse:
    """一次工具执行的结果（成功与失败共用一个信封）。"""
    ok: bool
    call_id: str = ""                        # Dispatcher 回填（Observation 配对键）
    data: Any = None                         # 结构化数据（程序消费）
    summary: str = ""                        # 给模型看的文本渲染
    error: ToolError | None = None
    artifacts: list[Artifact] = field(default_factory=list)

    _SUMMARY_LIMIT = 2000                    # 输出预算兜底（M07 做正式摘要）

    def render_for_model(self) -> str:
        """渲染成回填给模型的 Observation 文本。"""
        if self.ok:
            if len(self.summary) > self._SUMMARY_LIMIT:
                skipped = len(self.summary) - self._SUMMARY_LIMIT
                return self.summary[:self._SUMMARY_LIMIT] + f"\n...[truncated {skipped} chars]"
            return self.summary
        e = self.error
        hint = f"\n可采取的行动: {e.hint}" if e and e.hint else ""
        e = e or ToolError(ErrorKind.INTERNAL, "unknown", "未知错误")
        return f"[工具 {e.tool} 失败: {e.kind.value}] {e.message}{hint}"

    def render(self) -> str:
        """向后兼容别名（M03 的 loop/dispatcher 调用这个）。"""
        return self.render_for_model()
