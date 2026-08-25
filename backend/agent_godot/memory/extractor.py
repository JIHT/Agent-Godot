"""memory/extractor.py —— 会话复盘抽取 + 四路写入决策（M08 §1.2 / §3 / §4 步骤 3）

赛后复盘会：不是把比赛录像原样归档（全量存对话=检索噪音），
而是教练组开会提炼——"这场暴露的传中问题记到教训本"（semantic）、
"对方 7 号的边路突破存进录像摘要"（episodic）、互相击掌的庆祝不用记（寒暄）。

第一设计原则："能从代码库重新推导的事实不要记"——
记忆只存"推导不出来的"（偏好、理由、教训）。

四路写入决策（Mem0 核心）：ADD 全新 / UPDATE 合并 / DELETE 矛盾归档 / NOOP 重复。
批处理（抽取要批量省成本）与串行消解（决策要看到彼此效果）的张力——
用"批量抽取 + 邻域串行"折中，邻域 top=3 保证决策上下文足够小而准。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from ..core import LLM, LLMRequest, LLMResponse, Message
from .store import Embedder, MemoryRecord, MemoryStore

logger = logging.getLogger(__name__)

# ---------- 提示词 ----------

EXTRACT_PROMPT = """从以下会话纪要中抽取"值得长期记忆"的事实。

判据（只记推导不出来的——能从代码库重新推导的事实不要记）：
- 用户明确偏好/约定 → kind=semantic，importance 0.9+
- 踩坑与修复尤其版本差异类 → kind=episodic，importance 0.6
- 项目结构事实 → 忽略（由 profile 单独管，不进记忆表）
- 丢弃：寒暄/中间试错/可从代码库重新推导的事实

重要性标尺（违反标尺会导致召回噪音）：
- 0.9: 违反会导致返工的约定（缩进/命名/架构选择）
- 0.6: 踩坑经验/版本差异
- 0.5: 背景知识
- 0.3: 一般上下文

source 判据：
- user_stated: 用户明说的事实/偏好
- model_inferred: 模型从对话推断的（importance 上限 0.6）

输出 JSON 数组（无值得记的事则输出 []），每项：
{{"kind": "episodic|semantic", "content": "...", "importance": 0.0, "source": "user_stated|model_inferred"}}

会话纪要：
{digest}"""

DECIDE_PROMPT = """新事实: {fact}
已有相关记忆:
{existing}
决策（单选）:
- ADD: 全新信息，无重叠
- UPDATE: 与某条部分重叠且更准确/更新 → 给出合并后全文
- DELETE: 与某条矛盾且新事实为准 → 删除旧条
- NOOP: 重复信息
输出 JSON: {{"action": "ADD|UPDATE|DELETE|NOOP", "target_id": "...", "merged": "..."}}
（ADD/NOOP 时 target_id 和 merged 可省略）"""

# model_inferred 来源的 importance 硬上限
INFERRED_CAP = 0.6


@dataclass
class Decision:
    """四路决策结果。"""
    action: Literal["ADD", "UPDATE", "DELETE", "NOOP"]
    target_id: str | None = None
    merged: str | None = None
    reason: str = ""


@dataclass
class ExtractReport:
    """抽取审计报告：计数 + 明细（M19 落库，定期抽查廉价模型的错误率）。"""
    added: int = 0
    updated: int = 0
    deleted: int = 0
    noop: int = 0
    details: list[dict] = field(default_factory=list)

    def count(self, action: str) -> None:
        if action == "ADD":
            self.added += 1
        elif action == "UPDATE":
            self.updated += 1
        elif action == "DELETE":
            self.deleted += 1
        else:
            self.noop += 1

    @property
    def total(self) -> int:
        return self.added + self.updated + self.deleted + self.noop


class MemoryExtractor:
    """会话复盘抽取器：批量抽候选 → 逐条邻域消解 → 四路写入。"""

    def __init__(self, llm: LLM, embedder: Embedder,
                 store: MemoryStore,
                 compressor=None,
                 model: str = "",
                 project_id: str = "default"):
        self.llm = llm
        self.embedder: Embedder = embedder
        self.store = store
        self.compressor = compressor             # M07 Compressor（None → 用消息原文）
        self.model = model
        self.project_id = project_id

    # ---------- 主入口 ----------

    async def extract_from_session(self, session, *,
                                   project_id: str | None = None
                                   ) -> ExtractReport:
        """会话结束时的复盘：纪要 → 抽候选 → 逐条消解 → 写入。"""
        pid = project_id or self.project_id
        digest = await self._get_digest(session)
        candidates = await self._llm_extract(digest)
        report = ExtractReport()
        for cand in candidates:
            try:
                decision = await self._decide(pid, cand)
                await self._apply(pid, cand, decision, session, report)
            except Exception as e:                       # noqa: BLE001 —— 单条失败不拖死整批
                logger.warning("记忆抽取单条失败: %s（候选: %s）", e,
                               cand.get("content", "")[:80])
                report.details.append({
                    "candidate": cand, "error": str(e),
                })
        return report

    # ---------- 单条决策 + 执行（可独立调用，测试用） ----------

    async def decide_and_apply(self, project_id: str,
                               candidate: dict,
                               session_id: str | None = None) -> Decision:
        """单条决策+执行（测试 convenience）。"""
        report = ExtractReport()
        decision = await self._decide(project_id, candidate)
        await self._apply(project_id, candidate, decision,
                          _SessionStub(session_id), report)
        return decision

    # ---------- 内部：纪要 / 抽取 / 决策 / 执行 ----------

    async def _get_digest(self, session) -> str:
        """取纪要：优先 compressor.summarize（C 档），无 compressor 用消息 transcript。"""
        messages = getattr(session, "messages", [])
        if self.compressor is not None:
            summary = await self.compressor.summarize(messages, budget=1500)
            return summary.content or ""
        return self._transcript(messages)

    def _transcript(self, messages: list[Message]) -> str:
        """消息序列 → 紧凑 transcript（无 compressor 时的兜底原料）。"""
        out: list[str] = []
        for m in messages:
            if m.role == "user" and m.content:
                out.append(f"[用户] {m.content}")
            elif m.role == "assistant":
                if m.tool_calls:
                    names = ", ".join(tc.name for tc in m.tool_calls)
                    out.append(f"[助手→工具] {names}")
                if m.content:
                    out.append(f"[助手] {m.content[:300]}")
            elif m.role == "tool":
                out.append(f"[工具结果] {(m.content or '')[:200]}")
        return "\n".join(out) or "（空会话）"

    async def _llm_extract(self, digest: str) -> list[dict]:
        """一次调 LLM 抽 N 条候选（批量省成本）。"""
        if not digest.strip():
            return []
        prompt = EXTRACT_PROMPT.format(digest=digest)
        req = LLMRequest(
            model=self.model,
            messages=[Message(role="user", content=prompt)],
            temperature=0.1, tools=None)
        try:
            resp = await self.llm.complete(req)
            text = (resp.content or "").strip()
        except Exception as e:                     # noqa: BLE001 —— 抽取失败不拖死主流程
            logger.warning("LLM 抽取失败: %s", e)
            return []
        candidates = _parse_json_list(text)
        # model_inferred importance 硬上限（污染防线②）
        for c in candidates:
            if c.get("source") == "model_inferred":
                c["importance"] = min(float(c.get("importance", 0.5)), INFERRED_CAP)
        return candidates

    async def _decide(self, project_id: str, candidate: dict) -> Decision:
        """四路决策：检索邻域 top3 → DECIDE_PROMPT → 解析。"""
        content = candidate.get("content", "")
        if not content:
            return Decision(action="NOOP", reason="空内容")
        neighbors = await self.store.search_by_text(project_id, content, top=3)
        if not neighbors:
            return Decision(action="ADD")
        existing = "\n".join(
            f"- id={n.id} | {n.content}" for n in neighbors)
        prompt = DECIDE_PROMPT.format(fact=content, existing=existing)
        req = LLMRequest(
            model=self.model,
            messages=[Message(role="user", content=prompt)],
            temperature=0.1, tools=None)
        try:
            resp = await self.llm.complete(req)
            decision = _parse_decision(resp.content or "")
        except Exception as e:                     # noqa: BLE001 —— 决策失败默认 ADD（宁可重复也不丢信息）
            logger.warning("决策 LLM 失败，默认 ADD: %s", e)
            decision = Decision(action="ADD", reason=f"decide_failed: {e}")
        return decision

    async def _apply(self, project_id: str, candidate: dict,
                     decision: Decision, session, report: ExtractReport) -> None:
        """执行四路写入。"""
        action = decision.action
        report.count(action)
        detail = {"candidate": candidate, "decision": {
            "action": action, "target_id": decision.target_id,
            "merged": decision.merged, "reason": decision.reason}}
        report.details.append(detail)
        handler = getattr(self, f"_apply_{action.lower()}", None)
        if handler:
            await handler(project_id, candidate, decision, session)

    # ---------- 四路写入处理器 ----------

    async def _apply_add(self, project_id: str, candidate: dict,
                         decision: Decision, session) -> None:
        kind = candidate.get("kind", "semantic")
        if kind not in ("episodic", "semantic"):
            kind = "semantic"
        rec = MemoryRecord.make(
            kind=kind,
            content=candidate["content"],
            project_id=project_id,
            session_id=getattr(session, "session_id", None),
            importance=float(candidate.get("importance", 0.5)),
            source=candidate.get("source", "user_stated"),
            emb=self.embedder(candidate["content"]))
        await self.store.add(rec)

    async def _apply_update(self, project_id: str, candidate: dict,
                            decision: Decision, session) -> None:
        """UPDATE：merged 文本替换 + 重算 emb。附 diff 审计（矛盾消解决策本身是 LLM 可能误判）。"""
        if not decision.target_id:
            # 没给 target_id → 退化为 ADD
            await self._apply_add(project_id, candidate, decision, session)
            return
        old = await self.store.get_by_id(decision.target_id)
        if old is None:
            await self._apply_add(project_id, candidate, decision, session)
            return
        merged = decision.merged or candidate["content"]
        # 保留旧 importance（除非新值更高）
        new_imp = max(old.importance, float(candidate.get("importance", 0.5)))
        await self.store.update(decision.target_id, merged)
        # importance 可能变了 → 直接 SQL 更新
        if new_imp != old.importance:
            self.store.conn.execute(
                "UPDATE memories SET importance=? WHERE id=?",
                (new_imp, decision.target_id))
            self.store.conn.commit()

    async def _apply_delete(self, project_id: str, candidate: dict,
                            decision: Decision, session) -> None:
        """DELETE：矛盾归档（软删红线——archive 而非物理删）。"""
        if not decision.target_id:
            await self._apply_add(project_id, candidate, decision, session)
            return
        reason = (f"矛盾归档：被新事实替代。新事实={candidate['content'][:100]}；"
                  f"merged={decision.merged or '(无)'}")
        await self.store.archive(decision.target_id, reason=reason)
        # 归档后新事实 ADD 落库
        await self._apply_add(project_id, candidate, decision, session)

    async def _apply_noop(self, project_id: str, candidate: dict,
                          decision: Decision, session) -> None:
        """NOOP：跳过（重复信息不入库）。"""
        return


# ---------- JSON 解析工具 ----------

def _parse_json_list(text: str) -> list[dict]:
    """从 LLM 响应中提取 JSON 数组（容忍 markdown 包裹和额外文本）。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        end = len(lines) - 1
        if lines[end].startswith("```"):
            end -= 1
        text = "\n".join(lines[1:end + 1])
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass
    # 容错：找第一个 JSON 数组
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except json.JSONDecodeError:
            pass
    return []


def _parse_decision(text: str) -> Decision:
    """从 LLM 响应中解析四路决策 JSON。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        end = len(lines) - 1
        if lines[end].startswith("```"):
            end -= 1
        text = "\n".join(lines[1:end + 1])
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                data = {}
        else:
            data = {}
    action = str(data.get("action", "ADD")).upper().strip()
    if action not in ("ADD", "UPDATE", "DELETE", "NOOP"):
        action = "ADD"
    return Decision(
        action=action,                          # type: ignore[arg-type]
        target_id=data.get("target_id"),
        merged=data.get("merged"),
        reason=data.get("reason", ""))


class _SessionStub:
    """单条决策执行时的 session 替身（只需 session_id）。"""
    def __init__(self, session_id: str | None):
        self.session_id = session_id
        self.messages: list = []


__all__ = ["Decision", "ExtractReport", "MemoryExtractor"]
