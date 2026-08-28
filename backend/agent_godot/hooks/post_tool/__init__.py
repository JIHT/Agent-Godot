"""hooks/post_tool：工具执行后的 hook（可改写 Observation）。

- redact  (p=90，安全类)  ：凭据打码，防泄漏扩散进摘要/记忆/训练数据
- format  (p=100，业务类) ：.gd 落盘即格式化（缩进转 Tab / 空行归一）
执行序：redact → format（§1.1 ②"脱敏 → 格式化"）。
"""
from .format_hook import FormatHook, format_gdscript
from .redact_hook import RedactHook, redact_data, redact_text

__all__ = ["FormatHook", "format_gdscript",
           "RedactHook", "redact_data", "redact_text"]
