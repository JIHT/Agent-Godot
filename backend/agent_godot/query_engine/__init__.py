"""query_engine：意图 · 改写 · 路由 · Agentic RAG（M12）—— 医院分诊台

到 M11 为止，Agent 有了本地向量/图谱两条知识通道；M12 补齐联网通道
（web_provider）并回答"这次提问走哪条通道"：

- intent      分诊护士的三秒判断（五分类，few-shot 小模型）
- rewriter    导医把口语翻译成病历用语（指代消解 + HyDE 伪文档）
- router      挂号决策（意图+用户开关+多跳信号 → RoutePlan）
- pipeline    全链编排 + 多科室会诊报告（统一注入块，GRAPH>RAG>WEB）
- web_provider 120 外勤小队（搜索→择优抓取→正文抽取→安全信封）

交付后状态：产品设置三开关（联网/知识库/图谱）真正生效；追问句正确
改写；"改代码"与"问知识"按意图分流——知识系统三件套合体（MI-3 收官）。
"""
from .intent import INTENT_PROMPT, Intent, IntentClassifier
from .pipeline import QueryEngine, QueryResult, build_query_engine, rag_messages
from .rewriter import HYDE_PROMPT, REWRITE_PROMPT, QueryRewriter
from .router import (Channel, MULTI_JUMP, QueryRouter, RoutePlan,
                     RoutingConfig, RoutingContext, multi_hop_hint)
from .web_provider import (SearchEngine, SearchHit, SearxngEngine,
                           TavilyEngine, WebResult, WebSearchProvider,
                           domain_trust)

__all__ = [
    # intent
    "INTENT_PROMPT", "Intent", "IntentClassifier",
    # rewriter
    "HYDE_PROMPT", "REWRITE_PROMPT", "QueryRewriter",
    # router
    "Channel", "MULTI_JUMP", "QueryRouter", "RoutePlan", "RoutingConfig",
    "RoutingContext", "multi_hop_hint",
    # web
    "SearchEngine", "SearchHit", "SearxngEngine", "TavilyEngine",
    "WebResult", "WebSearchProvider", "domain_trust",
    # pipeline
    "QueryEngine", "QueryResult", "build_query_engine", "rag_messages",
]
