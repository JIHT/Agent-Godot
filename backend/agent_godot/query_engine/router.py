"""query_engine/router.py —— 规则+模型混合路由（M12 §1.3 / §4 步骤 3）

挂号决策：分诊结果（意图）+ 医院当天开放情况（用户开关）+ 患者预算，
综合决定挂哪些科。关键纪律——**停诊就是停诊**：用户关了知识库
（显式意愿），意图再像 knowledge（系统猜测）也不许偷偷挂号，
只能降级直答并明示"该科今日未开放"（信任优先，权限系统同款哲学）。

CODE_EDIT 不预取知识而交 craft 模式自决（M13）——检索时机交给 Loop
（Agentic 检索），避免"预取了一堆用不上"。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

from .intent import Intent

logger = logging.getLogger(__name__)

# 多跳信号词启发式："删这个信号影响哪些场景" → GRAPH 优先；
# "Area2D 和 StaticBody2D 区别" → RAG（对比类文档）。
MULTI_JUMP = re.compile(r"(哪些|影响|依赖|用到|引用|之间|关系|区别.*和|和.*区别)")


def multi_hop_hint(query: str) -> bool:
    """问题里是否有多跳信号词（pipeline 上下文未显式给时自动检测）。"""
    return bool(MULTI_JUMP.search(query or ""))


class Channel(Enum):
    RAG = "rag"            # 知识库（通用文档，参考）
    GRAPH = "graph"        # 项目图谱（你的项目的事实，权威）
    WEB = "web"            # 联网（时效信息，预算最紧）
    LLM_DIRECT = "llm"     # 全关/不适用 → 明示直答


@dataclass
class RoutingConfig:
    """各通道默认预算（RoutePlan.budget 的取值来源）。"""
    rag_top_k: int = 3          # RAG 命中数（带引用的 chunk 是回答主力）
    web_n: int = 5              # 联网搜索条数（只用前 3 页正文）


@dataclass
class RoutingContext:
    """一次路由决策的现场：用户开关（硬约束）+ 项目状态 + 检测信号。"""
    kb_enabled: bool = True            # 产品设置：知识库检索开关
    web_enabled: bool = False          # 产品设置：联网搜索开关
    graph_ready: bool = False          # 项目图已建才走 GRAPH
    project_id: str = ""               # GRAPH 通道执行需要
    multi_hop_hint: bool | None = None # None = 由 router 用信号词检测


@dataclass
class RoutePlan:
    """通道执行计划：挂哪些科、怎么挂、为什么（reason 落 trace 排障全靠它）。"""
    channels: list[Channel] = field(default_factory=list)
    mode: str | None = None            # code_edit → "craft"（M13 接棒）
    reason: str = ""
    budget: dict[str, int] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)   # trace 用


class QueryRouter:
    """§1.3 矩阵代码化：意图四分支 + 开关硬约束 + 多跳信号词。"""

    def __init__(self, config: RoutingConfig | None = None):
        self.config = config or RoutingConfig()

    def decide(self, intent: Intent, ctx: RoutingContext) -> RoutePlan:
        b = self.config
        meta = {"intent": intent.value}

        # ---- 闲聊：空通道直答（省钱省延迟）----
        if intent is Intent.CHITCHAT:
            return RoutePlan([], mode=None,
                             reason="闲聊意图：直答，零检索", meta=meta)

        # ---- 改代码：交 craft 模式（M13），Loop 内自决检索 ----
        if intent is Intent.CODE_EDIT:
            return RoutePlan([], mode="craft", meta=meta,
                             reason="改代码意图：交 craft 模式，"
                                    "Loop 内自决检索（Agentic）")

        # ---- 时效性查询：联网优先；联网停诊则降级知识通道 ----
        if intent is Intent.SEARCH:
            if ctx.web_enabled:
                return RoutePlan([Channel.WEB], meta=meta,
                                 budget={"web_n": b.web_n},
                                 reason="时效性查询：联网优先")
            plan = self._knowledge_plan(ctx, meta, web_off=True)
            plan.reason = f"联网未启用，search 意图降级；{plan.reason}"
            return plan

        # ---- 知识/未知/仍模糊：保守默认全部走 knowledge 矩阵 ----
        return self._knowledge_plan(ctx, meta)

    # ---------- knowledge 矩阵（含 unknown/ambiguous 保守默认） ----------

    def _knowledge_plan(self, ctx: RoutingContext, meta: dict,
                        web_off: bool = False) -> RoutePlan:
        b = self.config
        channels: list[Channel] = []
        notes: list[str] = []

        hint = ctx.multi_hop_hint
        if ctx.kb_enabled:
            channels.append(Channel.RAG)
        else:
            notes.append("知识库未启用")
        if ctx.graph_ready:
            channels.append(Channel.GRAPH)
        else:
            notes.append("项目图谱未就绪")

        # 多跳信号：GRAPH 优先（项目事实权威），RAG 补文档
        if hint and Channel.GRAPH in channels and Channel.RAG in channels:
            channels = [Channel.GRAPH, Channel.RAG]

        if not channels:
            channels = [Channel.LLM_DIRECT]
            return RoutePlan(
                channels, meta=meta,
                reason="、".join(notes) + "：明示直答（答案可能不含"
                       "私有文档与最新信息）")

        reason = "knowledge 意图" + ("含多跳信号词，图谱优先" if hint else "")
        if notes:
            reason += "；" + "、".join(notes)
        return RoutePlan(channels, meta=meta,
                         budget={"rag_top_k": b.rag_top_k}, reason=reason)


__all__ = ["Channel", "MULTI_JUMP", "QueryRouter", "RoutePlan",
           "RoutingConfig", "RoutingContext", "multi_hop_hint"]
