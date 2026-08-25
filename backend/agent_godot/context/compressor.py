"""context/compressor.py —— 摘要压缩三档（M07 §1.5 / §4 步骤 5）

- A 档（粗）删除法：工具轮整体占位 —— HistoryManager.sweep 共用，不在本文件；
- B 档（中）模板摘要：规则抽取每轮"用户意图 + 最终结果"，零 LLM 成本；
- C 档（细）LLM 摘要：廉价模型生成五段结构化纪要（目标/决策/已改/未决/偏好）。

压缩不是删光：注入一条 system 摘要消息宣告"完整原文已省略"。
摘要里的文件清单必须带 hash——那是压缩后的会话与文件系统现实之间
仅存的版本锚点（M04 乐观锁恢复执行要对版本）。
"""
from __future__ import annotations

from ..core import LLMRequest, Message

COMPRESS_PROMPT = """把以下 Agent 会话片段压缩为结构化纪要，供后续继续执行任务时参考。
要求：
- 保留：任务目标、关键决策及理由、已修改文件清单（含 hash）、未完成事项、用户明确偏好
- 丢弃：工具调用过程细节、中间试探、失败尝试（除非失败原因影响后续）
- 输出格式：目标/决策/已改/未决/偏好 五段，总长 ≤ {budget} tokens
片段：
{transcript}"""


class Compressor:
    """B 档顺手做（每轮 sweep）、C 档低频做（阈值触发/任务边界）。"""

    def __init__(self, llm=None, model: str = "", temperature: float = 0.1):
        self.llm = llm                     # None → C 档自动降级为 B 档
        self.model = model
        self.temperature = temperature

    # ---------- B 档：模板摘要（零成本） ----------

    def template_digest(self, messages: list[Message]) -> Message:
        """每轮抽 user 消息首行 + assistant 纯文本尾行，拼两行式纪要。"""
        lines: list[str] = []
        for m in messages:
            if m.role == "user" and m.content:
                lines.append(f"用户: {m.content.strip().splitlines()[0][:100]}")
            elif m.role == "assistant":
                if m.tool_calls:
                    names = ", ".join(tc.name for tc in m.tool_calls)
                    lines.append(f"助手: 调用工具 {names}")
                elif m.content:
                    lines.append(f"助手: {m.content.strip().splitlines()[-1][:100]}")
        digest = "\n".join(lines) or "（空片段）"
        return Message(
            role="system",
            content=f"<summary mode=template>以下为此前对话的摘要，"
                    f"完整原文已省略：\n{digest}</summary>")

    # ---------- C 档：LLM 摘要（高保真） ----------

    async def summarize(self, messages: list[Message],
                        budget: int = 1500) -> Message:
        """调廉价模型生成结构化纪要；无 LLM 或调用失败 → B 档兜底。"""
        if not messages:
            return Message(role="system", content="<summary>（空片段）</summary>")
        if self.llm is None:
            return self.template_digest(messages)
        prompt = COMPRESS_PROMPT.format(budget=budget,
                                        transcript=self._transcript(messages))
        req = LLMRequest(model=self.model,
                         messages=[Message(role="user", content=prompt)],
                         temperature=self.temperature, tools=None)
        try:
            resp = await self.llm.complete(req)
            text = (resp.content or "").strip()
        except Exception:                  # noqa: BLE001 —— 压缩失败不拖死主流程
            text = ""
        if not text:
            return self.template_digest(messages)
        return Message(
            role="system",
            content=f"<summary mode=llm>以下为此前对话的摘要，"
                    f"完整原文已省略：\n{text}</summary>")

    def _transcript(self, messages: list[Message]) -> str:
        """消息序列 → 紧凑 transcript（喂给摘要 prompt 的原料）。"""
        out: list[str] = []
        for m in messages:
            if m.role == "user":
                out.append(f"[用户] {m.content}")
            elif m.role == "assistant":
                for tc in m.tool_calls or []:
                    out.append(f"[助手→工具] {tc.name}({(tc.arguments or '')[:200]})")
                if m.content:
                    out.append(f"[助手] {m.content[:300]}")
            elif m.role == "tool":
                out.append(f"[工具结果] {(m.content or '')[:200]}")
            else:
                out.append(f"[系统] {(m.content or '')[:200]}")
        return "\n".join(out)
