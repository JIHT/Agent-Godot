"""context/token_counter.py —— 行李箱秤（M07 §1.1 / §4 步骤 1）

双模设计：估算常行（启发式，O(len) 零成本）+ 临界精算（tiktoken，惰性缓存）。
每次真实响应的 usage.input_tokens 是"电表读数"——calibrate 用它回归校准估算系数
（误差主要来自文本语言构成的系统性偏差，回归恰好治这个）。

大白话：收拾行李不每放一件就上秤——目测（估算）够用；只在接近 23kg 限重线时
认真上秤（精算）；每次托运后拿机场秤的实际读数修正目测手感（校准）。
"""
from __future__ import annotations

import json

from ..core import Message, ToolSpec

DEFAULT_CJK_RATIO = 0.6    # 中文 ≈0.6 token/字
NON_CJK_PER_TOKEN = 4      # 非中文 ≈0.25 token/字符
STRUCT_OVERHEAD = 4        # 每消息结构开销（role/分隔符）
TAIL_OVERHEAD = 2          # 请求结尾的 priming 开销
CALIB_WINDOW = 8           # 滑动平均窗口（防单次抖动过拟合）
CALIB_STEP = 0.05          # 每次调整幅度


class TokenCounter:
    """上下文工程一切决策（截断/压缩/预算）的计量底座。"""

    def __init__(self, cjk_ratio: float = DEFAULT_CJK_RATIO):
        self.cjk_ratio = cjk_ratio
        self._encoder = None            # tiktoken 编码器（惰性缓存，重复 get_encoding 很慢）
        self._encoder_tried = False
        self._recent_errors: list[float] = []   # (reported - estimated) / reported

    # ---------- 估算（常行） ----------

    def estimate_text(self, text: str | None) -> int:
        """单段文本估算：中文按 cjk_ratio，其余按 4 字符/token。"""
        if not text:
            return 0
        cjk = sum("\u4e00" <= ch <= "\u9fff" for ch in text)
        return int(cjk * self.cjk_ratio + (len(text) - cjk) / NON_CJK_PER_TOKEN)

    def estimate(self, messages: list[Message]) -> int:
        """消息序列估算：结构开销 + content + tool_calls 的 arguments。"""
        total = TAIL_OVERHEAD
        for m in messages:
            total += STRUCT_OVERHEAD
            total += self.estimate_text(m.content)
            if m.tool_calls:
                # 工具调用的 arguments 也占输入 token（name/包装 ≈8）
                total += sum(len(tc.arguments or "") // NON_CJK_PER_TOKEN + 8
                             for tc in m.tool_calls)
        return total

    def estimate_tools(self, tools: list[ToolSpec] | None) -> int:
        """工具声明（tools 参数）随每次请求完整发送并计入 input_tokens。

        一个中型 MCP 服务器桥接进来可能吃 2~4k——预算必须按实测值扣减，
        而不是拍脑袋给固定额度。
        """
        if not tools:
            return 0
        total = 0
        for spec in tools:
            total += 16      # name + FC 包装结构
            total += self.estimate_text(spec.description)
            try:
                total += self.estimate_text(
                    json.dumps(spec.parameters, ensure_ascii=False))
            except (TypeError, ValueError):
                total += 64
        return total

    # ---------- 精算（临界兜底） ----------

    def exact(self, messages: list[Message], model: str = "") -> int:
        """tiktoken 精算；不可用（未安装/离线）时退回估算——秤坏了目测总得有。"""
        enc = self._get_encoder()
        if enc is None:
            return self.estimate(messages)
        total = TAIL_OVERHEAD
        for m in messages:
            total += STRUCT_OVERHEAD + len(enc.encode(m.content or ""))
            if m.tool_calls:
                for tc in m.tool_calls:
                    total += len(enc.encode(tc.arguments or "")) + 8
        return total

    def _get_encoder(self):
        if self._encoder_tried:
            return self._encoder
        self._encoder_tried = True
        try:                                # tiktoken 是可选依赖：缺席不致命
            import tiktoken
            self._encoder = tiktoken.get_encoding("o200k_base")
        except Exception:                   # noqa: BLE001 —— 导入失败/无词表皆走兜底
            self._encoder = None
        return self._encoder

    # ---------- 自校准（usage 回执回归） ----------

    def calibrate(self, reported_input_tokens: int, estimated: int) -> float:
        """用真实 usage 回执校准估算系数。

        误差率 = (reported - estimated) / reported；|误差| > 10% 时按滑动均值
        方向把中文系数 ±0.05（低估 → 调大）。返回当前系数。
        """
        if reported_input_tokens <= 0 or estimated <= 0:
            return self.cjk_ratio
        err = (reported_input_tokens - estimated) / reported_input_tokens
        self._recent_errors.append(err)
        if len(self._recent_errors) > CALIB_WINDOW:
            self._recent_errors.pop(0)
        if abs(err) <= 0.10:               # 误差可接受：不动（防阈值附近抖动）
            return self.cjk_ratio
        avg = sum(self._recent_errors) / len(self._recent_errors)
        step = CALIB_STEP if avg > 0 else -CALIB_STEP
        self.cjk_ratio = min(1.5, max(0.2, self.cjk_ratio + step))
        return self.cjk_ratio
