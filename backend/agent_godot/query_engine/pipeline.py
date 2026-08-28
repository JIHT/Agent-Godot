"""query_engine/pipeline.py —— 全链编排 + 整合注入（M12 §1.4 / §3 / §4 步骤 4-5）

分诊台本尊：意图分类 → （ambiguous 回路：改写→二次分类→保守默认）
→ 路由决策 → 通道 asyncio.gather 并行 → 结果整合（GRAPH>RAG>WEB
去重 + 预算截断）→ QueryResult（五段决策落 trace）。

§3 难点——ambiguous 的消解回路（改写与分类的循环依赖）：
用"一次改写 + 一次二次分类"封顶打破环，仍模糊就落保守默认
（knowledge：只读无副作用、覆盖面最广、Loop 内出口还开着）。

小模型场景装配：build_query_engine(registry, ...) —— intent/rewrite
用 registry.llm_for_task 装配，models.yaml 未配回落 ask 主 LLM。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any

from ..core import Message
from ..graphrag import GraphPath, GraphVectorFusion
from ..rag import HybridRetriever, RetrievalHit, CITE_PROMPT
from ..rag.citation import CitationFormatter
from .intent import Intent, IntentClassifier
from .rewriter import QueryRewriter
from .router import (Channel, QueryRouter, RoutePlan, RoutingConfig,
                     RoutingContext, multi_hop_hint)
from .web_provider import WebSearchProvider

logger = logging.getLogger(__name__)

# 注入总预算受 M07 分区钳制（4k token）：按 CJK≈1 token/1.6 chars 粗估
_INJECT_BUDGET_TOKENS = 4000


def _est_tokens(text: str) -> int:
    """粗估 token（中英混排：chars/2 与 CJK 密度折中，够做预算钳制）。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + (len(text) - cjk) // 4


@dataclass
class QueryResult:
    """一次 process 的全部产出：五段决策 + 注入块。"""
    intent: Intent
    rewritten: str
    plan: RoutePlan
    context_block: str                    # 进 ContextBuilder rag 分区的注入块
    elapsed_ms: int
    trace: dict = field(default_factory=dict)


class QueryEngine:
    """医院分诊台：process() 一进一出，全链决策落 trace。"""

    def __init__(self, classifier: IntentClassifier, rewriter: QueryRewriter,
                 router: QueryRouter, rag: HybridRetriever,
                 graph: GraphVectorFusion, web: WebSearchProvider | None,
                 llm=None,
                 inject_budget_tokens: int = _INJECT_BUDGET_TOKENS,
                 hyde_enabled: bool = False):
        self.classifier = classifier
        self.rewriter = rewriter
        self.router = router
        self.rag = rag                     # HybridRetriever（M10）
        self.graph = graph                 # GraphVectorFusion / ProjectGraphSync
        self.web = web                     # WebSearchProvider（M12 §1.5）
        self.llm = llm                     # 兼容签名保留（暂不直用）
        self.inject_budget_tokens = inject_budget_tokens
        self.hyde_enabled = hyde_enabled

    # ---------------------------------------------------------- 主入口

    async def process(self, input: str, history: list[Message] | None,
                      ctx: RoutingContext) -> QueryResult:
        t0 = time.perf_counter()
        trace: dict[str, Any] = {}

        # ① 意图分类
        intent = await self.classifier.classify(input, history or [])
        trace["intent"] = intent.value

        # ② ambiguous 消解回路（§3）：改写 → 二次分类 → 仍模糊落保守默认
        if intent is Intent.AMBIGUOUS:
            rewritten = await self.rewriter.rewrite(input, history or [])
            intent = await self.classifier.classify(rewritten, history or [])
            trace["intent_2nd"] = intent.value
            trace["rewritten"] = rewritten
            if intent in (Intent.AMBIGUOUS, Intent.UNKNOWN):
                intent = Intent.KNOWLEDGE
                trace["intent_fallback"] = "knowledge（保守默认）"
        else:
            rewritten = await self.rewriter.rewrite(input, history or [])
            trace["rewritten"] = rewritten

        # ③ 路由决策（多跳信号未显式给 → 用信号词启发式检测）
        if ctx.multi_hop_hint is None:
            ctx = replace(ctx, multi_hop_hint=multi_hop_hint(rewritten))
        plan = self.router.decide(intent, ctx)
        trace["reason"] = plan.reason

        # ④ 通道并行执行（HyDE 可选：只影响 RAG 检索句）
        rag_query = rewritten
        if self.hyde_enabled and Channel.RAG in plan.channels:
            rag_query = await self.rewriter.hyde(rewritten)
            trace["hyde"] = rag_query
        results, stats = await self._execute(rag_query, rewritten, plan, ctx)
        trace["channels"] = stats

        # ⑤ 结果整合（GRAPH>RAG>WEB 去重 + 预算截断）
        context_block = self.consolidate(rewritten, plan, results)
        trace["inject_tokens"] = _est_tokens(context_block)
        trace["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)

        return QueryResult(intent=intent, rewritten=rewritten, plan=plan,
                           context_block=context_block,
                           elapsed_ms=trace["elapsed_ms"], trace=trace)

    # ---------------------------------------------------------- 通道执行

    async def _execute(self, rag_query: str, graph_query: str,
                       plan: RoutePlan, ctx: RoutingContext
                       ) -> tuple[dict[Channel, Any], list[dict]]:
        """各通道并行（gather），单通道 fail-soft 降级空结果不炸整体。"""
        jobs: list[tuple[Channel, Any]] = []
        for ch in plan.channels:
            if ch is Channel.RAG:
                jobs.append((ch, self.rag.retrieve(
                    rag_query, top_k=plan.budget.get("rag_top_k", 3))))
            elif ch is Channel.GRAPH:
                jobs.append((ch, self.graph.trace(
                    graph_query, ctx.project_id)))
            elif ch is Channel.WEB and self.web is not None:
                jobs.append((ch, self.web.gather(
                    graph_query, n=plan.budget.get("web_n", 5))))
            elif ch is Channel.LLM_DIRECT:
                jobs.append((ch, None))

        stats: list[dict] = []
        results: dict[Channel, Any] = {}

        async def run(ch: Channel, coro) -> None:
            t0 = time.perf_counter()
            try:
                out = await coro
                ok, err = True, ""
            except Exception as e:             # noqa: BLE001 —— 通道降级不崩
                logger.warning("通道 %s 降级（%s）", ch.value,
                               type(e).__name__)
                out, ok, err = [], False, type(e).__name__
            results[ch] = out
            stats.append({
                "channel": ch.value, "ok": ok, "err": err,
                "count": len(out) if isinstance(out, list) else 0,
                "ms": int((time.perf_counter() - t0) * 1000)})

        if jobs:
            await asyncio.gather(*(run(ch, coro) for ch, coro in jobs))
        return results, stats

    # ---------------------------------------------------------- 结果整合

    def consolidate(self, rewritten: str, plan: RoutePlan,
                    results: dict[Channel, Any]) -> str:
        """多科室会诊报告：统一注入块（§1.4 格式），GRAPH>RAG>WEB。

        - 空结果通道渲染"（无结果）"、未启用通道渲染"（未启用）"——
          空块会诱发模型脑补，明示状态比留白干净；
        - RAG 按文档去重；超预算按通道优先级从后往前截（WEB→RAG）。
        """
        if not plan.channels:
            return ""          # 闲聊直答 / craft 交 M13：都不预注入
        if plan.channels == [Channel.LLM_DIRECT]:
            return self._direct_note(plan)

        blocks: list[str] = []
        seen_docs: set[str] = set()

        for ch, title in ((Channel.GRAPH, "项目图谱"), (Channel.RAG, "知识库"),
                          (Channel.WEB, "联网")):
            items = results.get(ch)
            if ch not in plan.channels:
                blocks.append(f"[{title}]（未启用）")
                continue
            if not items:
                blocks.append(f"[{title}]（无结果）")
                continue
            if ch is Channel.GRAPH:
                blocks.append(f"[{title}]\n推理链：\n" + "\n".join(
                    p.render() for p in items))
            elif ch is Channel.RAG:
                hits = [h for h in items
                        if h.chunk.doc_id not in seen_docs]
                seen_docs.update(h.chunk.doc_id for h in hits)
                blocks.append(f"[{title}]\n"
                              + self._render_rag(hits))
            elif ch is Channel.WEB:
                blocks.append(f"[{title}]\n" + "\n".join(
                    self._render_web(r, i) for i, r in enumerate(items, 1)))

        router_desc = "+".join(c.value.upper() for c in plan.channels)
        body = "\n".join(blocks)
        head = (f'<retrieved_context query="{_esc(rewritten)}" '
                f'router="{router_desc}" reason="{_esc(plan.reason)}">')
        block = f"{head}\n{body}\n</retrieved_context>"

        # 预算钳制：超了按 GRAPH>RAG>WEB 优先级从后往前硬截（保项目事实）
        budget = self.inject_budget_tokens
        if _est_tokens(block) > budget:
            logger.info("注入块超预算（%d>%d tokens），按优先级截断",
                        _est_tokens(block), budget)
            keep = block[: budget * 2]      # 粗估截断（chars ≈ 2×tokens）
            block = keep[: keep.rfind("\n")] + "\n…（超预算截断）\n</retrieved_context>"
        return block

    # ---------- 渲染细节 ----------

    def _render_rag(self, hits: list[RetrievalHit]) -> str:
        return CitationFormatter().render_context(hits)

    @staticmethod
    def _render_web(r, i: int) -> str:
        head = f"[{i}] {r.title}\n    {r.url}"
        if r.content:
            indent = "\n    ".join(r.content.splitlines())
            return f"{head}\n    {indent}"
        return f"{head}\n    （未抓到正文，摘要：{r.snippet[:200]}）"

    @staticmethod
    def _direct_note(plan: RoutePlan) -> str:
        return (f'<retrieved_context router="LLM_DIRECT" '
                f'reason="{_esc(plan.reason)}">\n'
                f"本次未启用知识检索，直答模式——答案可能不含你的私有文档"
                f"或最新信息。\n</retrieved_context>")


def _esc(s: str) -> str:
    return (s or "").replace('"', "'").replace("\n", " ")[:200]


# ---------------------------------------------------------- 接线（§4 步骤 5）

def rag_messages(result: QueryResult) -> list[Message]:
    """QueryResult → ContextBuilder rag 分区的注入消息（M03 前置接线）。

    RAG 通道命中时附 CITE_PROMPT（引用约束与 [n] 编号体系配套）。
    """
    if not result.context_block:
        return []
    content = result.context_block
    if Channel.RAG in result.plan.channels:
        content = content + "\n\n" + CITE_PROMPT
    return [Message(role="system", content=content)]


# ---------------------------------------------------------- 工厂：小模型装配

def build_query_engine(registry, *, rag: HybridRetriever,
                       graph: GraphVectorFusion,
                       web_engine=None,
                       routing_config: RoutingConfig | None = None,
                       hyde_enabled: bool = False,
                       inject_budget_tokens: int = _INJECT_BUDGET_TOKENS
                       ) -> QueryEngine:
    """从 ModelRegistry 装配 QueryEngine（小模型场景自由配置的落点）。

    - intent / rewrite（/ hyde）各走 registry.llm_for_task(<role>)：
      models.yaml 的 routing 里配了同名键就用专属小模型，
      没配就回落 ask 主 LLM——"默认都是主 LLM，用户可自行配置"；
    - web_engine：SearchEngine 实现（TavilyEngine/SearxngEngine/Mock），
      None = 联网通道不装配（RoutingContext.web_enabled 应同步关）。
    """
    def role(name: str, default_t: float):
        rule = registry.task_rule(name)
        cfg = registry.get(rule.ref)
        t = rule.temperature if rule.temperature is not None else default_t
        mt = rule.max_tokens
        return registry.llm(rule.ref), cfg.model, t, mt

    intent_llm, intent_model, intent_t, intent_mt = role("intent", 0.0)
    rewrite_llm, rewrite_model, rewrite_t, rewrite_mt = role("rewrite", 0.2)
    classifier = IntentClassifier(
        intent_llm, intent_model, temperature=intent_t,
        max_tokens=intent_mt or 16)
    rewriter = QueryRewriter(
        rewrite_llm, rewrite_model, temperature=rewrite_t,
        max_tokens=rewrite_mt or 256)
    web = WebSearchProvider(web_engine) if web_engine is not None else None
    return QueryEngine(classifier, rewriter, QueryRouter(routing_config),
                       rag, graph, web,
                       inject_budget_tokens=inject_budget_tokens,
                       hyde_enabled=hyde_enabled)


__all__ = ["QueryEngine", "QueryResult", "build_query_engine", "rag_messages"]
