"""query_engine/rewriter.py —— 指代消解 + HyDE（M12 §1.2 / §4 步骤 2）

导医把口语翻译成病历用语：患者说"那它呢"——病历上不能这么写，
要结合上一句（聊的是 Area2D）誊成"Area2D 的信号列表"。

两条铁律：
- 检索系统无状态（只看当前查询串）——"那它的信号呢"直接检索=空指针查库；
- 形态不对称损耗：语料库存的是答案形态，用户输入是问题形态——改写把
  查询推向文档形态，HyDE 是极致（直接生成伪文档拿去检索）。

省钱机关：_needs_rewrite 的 fast-path 很值钱——完整独立句占比过半，
跳过改写省一次调用与 300ms。

小模型场景：本改写器用 registry.llm_for_task("rewrite") 装配
（HyDE 走 hyde 角色）——未配置回落 ask 主 LLM。
"""
from __future__ import annotations

import logging
import re

from ..core import LLMRequest, LLMResponse, Message

logger = logging.getLogger(__name__)

REWRITE_PROMPT = """基于对话历史，把用户最新输入改写成独立、完整、适合检索的查询。
只改写不回答：保留用户原意与技术词，补全代词与省略所指，输出一行查询。
历史：
{history}
最新输入：{input}
检索查询："""

HYDE_PROMPT = """针对下面的检索查询，写一段"假想中的完美答案"（伪文档）：
- 80~150 字的陈述句，形态与用词贴近真实答案文档；
- 包含查询涉及的关键术语；不确定的事实按最常见情况写；
- 只输出伪文档本身，不要解释你在做什么。
检索查询：{query}
伪文档："""

# 指代/省略信号词：命中即需要上下文补全
_PRONOUN_RE = re.compile(
    r"(这个|那个|它|他|她|这个呢|那.*呢|前面|上面|刚才|继续|"
    r"第二个|另一个|也一样|具体说说|展开讲)")
# 过短输入大概率是省略句（"呢？"/"然后呢"）
_MIN_INDEPENDENT_LEN = 8


class QueryRewriter:
    """改写器：fast-path 跳过完整句，代词句补全，HyDE 可选。"""

    def __init__(self, llm, model: str, temperature: float = 0.2,
                 max_tokens: int = 256, digest_turns: int = 3):
        self.llm = llm                     # registry.llm_for_task("rewrite")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.digest_turns = digest_turns   # 近 3 轮上下文够消解指代

    # ---------- 指代消解 ----------

    def _needs_rewrite(self, input: str) -> bool:
        """fast-path：无代词/省略的完整句跳过（省一次调用与 300ms）。"""
        text = (input or "").strip()
        if not text:
            return False
        if len(text) < _MIN_INDEPENDENT_LEN:
            return True                     # 过短 = 大概率省略句
        return bool(_PRONOUN_RE.search(text))

    async def rewrite(self, input: str, history: list[Message] | None = None
                      ) -> str:
        """指代消解改写。改写失败/无历史 → 原样返回（fail-soft）。"""
        if not self._needs_rewrite(input):
            return input
        if not history:
            return input                    # 无上文可消解，改写无从下手
        try:
            resp: LLMResponse = await self.llm.complete(LLMRequest(
                model=self.model,
                messages=[Message(role="user", content=REWRITE_PROMPT.format(
                    history=self._digest(history), input=input))],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                stream=False))
            text = (resp.content or "").strip()
            # 只取第一行：模型偶尔"顺手"多解释，改写要的是一行查询
            if text:
                return text.splitlines()[0].strip()
        except Exception as e:              # noqa: BLE001 —— 改写失败不阻塞
            logger.warning("查询改写失败（%s），用原句检索", e)
        return input

    # ---------- HyDE：用答案找答案 ----------

    async def hyde(self, query: str) -> str:
        """生成假想答案文档（伪文档）拿去检索——形态与库内文档对齐。

        收益场景：术语鸿沟大（口语 vs 文档术语）；查询已含准确 API 名
        时纯属画蛇添足，由 pipeline 的开关控制是否启用。
        """
        try:
            resp: LLMResponse = await self.llm.complete(LLMRequest(
                model=self.model,
                messages=[Message(role="user",
                                  content=HYDE_PROMPT.format(query=query))],
                temperature=self.temperature + 0.1,
                max_tokens=self.max_tokens,
                stream=False))
            text = (resp.content or "").strip()
            return text or query
        except Exception as e:              # noqa: BLE001 —— HyDE 失败用原查询
            logger.warning("HyDE 生成失败（%s），用原查询检索", e)
            return query

    # ---------- 内部 ----------

    def _digest(self, history: list[Message]) -> str:
        """近 N 轮的紧凑摘录（喂给改写 prompt 的原料，每行截断防肥）。"""
        lines: list[str] = []
        recent = [m for m in history if m.content][-2 * self.digest_turns:]
        for m in recent:
            who = "用户" if m.role == "user" else "助手"
            first = m.content.strip().splitlines()[0][:150]
            lines.append(f"[{who}] {first}")
        return "\n".join(lines) or "（无历史）"


__all__ = ["HYDE_PROMPT", "REWRITE_PROMPT", "QueryRewriter"]
