# M12 Query Engine（意图 · 改写 · 路由 · Agentic RAG）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 8 · 里程碑 MI-3「知识三件套齐」收官 |
| 代码落点 | `backend/agent_godot/query_engine/`（intent/rewriter/router/pipeline） |
| 前置模块 | M10（检索原语）· M11（图查询原语）· M03（Loop 注入检索结果的位置） |
| 手写比例 | 100% 手写 |
| 教程映射 | 📘 zero2Agent 03 课（编排半场——M02 是接入半场，本模块合拢）· 📝笔记 Query Engine/Agentic RAG |

---

## 0. 本模块在项目中的位置

到 M11 为止，Agent 有了多种"知识获取通道"（本地向量/图谱/联网/直接问模型），**但谁来决定这次提问走哪条通道？** Query Engine 是用户输入后的第一个决策点：

```text
用户输入 → [意图分类] → [查询改写] → [路由决策] → 各引擎执行 → [结果整合] → 进入 Agent Loop
```

不做这个决策层的后果：每次提问全通道齐开（成本×4、延迟×4、噪音×N）或全靠模型默认（该检索的不检索——幻觉重灾区）。

**交付后状态**：产品设置里的三个开关（联网搜索 / 知识库检索 / 本地模型）真正生效；追问句正确改写；"改代码"与"问知识"分流——MI-3 收官，知识系统三件套（RAG/Graph/Query）合体。

---

## 1. 知识点详解

### 1.1 意图分类：第一道岔路

**① 原理**

意图 = 用户想干什么，决定后续全链路。本项目五分类（few-shot 分类器，小模型执行）：

```text
code_edit    "给玩家加双跳"           → craft 模式（M13），不需要知识检索前置
knowledge    "Area2D 和 StaticBody2D 区别" → RAG/图谱通道，只读问答
chitchat     "谢谢/你叫什么"           → 直答，零检索（省钱省延迟）
search       "Godot 4.4 最新特性"     → 联网通道（时效性）
ambiguous    "那这个怎么用？"          → 上下文消解后二次分类
```

实现三选型对比（面试高频）：

| 方案 | 延迟 | 成本 | 准确率 | 本项目 |
|---|---|---|---|---|
| 规则关键词 | 0ms | 0 | 低（"加"字误伤无数） | 兜底 fast-path |
| 小模型分类（deepseek-chat few-shot） | ~300ms | 极低 | 高 | ★主分类器 |
| 嵌入相似度（意图样例库向量近邻） | ~50ms | 极低 | 中 | 缓存层/降级 |

**② 演进**：无分类（全走一条路）→ 规则 → BERT 式小分类器（要训练）→ LLM few-shot（零训练、换意图只改提示）→ 混合（规则 fast-path + LLM 兜底）。趋势：**分类能力"外包"给 LLM，工程只管提示与缓存**。

**③ 最小案例**：few-shot 分类提示

```python
INTENT_PROMPT = """判断用户输入的意图，只输出标签：
code_edit=修改/创建项目代码 | knowledge=询问 Godot/引擎知识 | chitchat=闲聊
| search=时效性查询 | ambiguous=依赖上下文

代码不用改时优先 knowledge。示例：
"给敌人加AI巡逻" → code_edit
"信号和回调的区别" → knowledge
"帮我看看 latest stable 版本是" → search
"好的谢谢" → chitchat
"那第二个呢" → ambiguous

输入：{input}
标签："""
```

**④ 易错点**
- 意图集会演化（M16 加 voice_transcribe）——分类器输出要留 `unknown` 出口路由到保守默认（knowledge）
- code_edit/knowledge 边界靠"是否动项目文件"判据写明在提示里，否则"看看 player.gd 有没有 bug"（要读代码但问答）两头摆
- 分类结果缓存：同句重复输入（用户重发）直接吃缓存，省一次调用

### 1.2 查询改写：把"对话碎片"变成"独立检索句"

**① 原理**

检索系统是**无状态的**——它只看当前查询字符串。而用户的提问充满上下文依赖：

```text
会话上文：Area2D 怎么检测碰撞？
用户追问："那它的信号呢？"              → 直接检索"它的信号"=垃圾
改写后："Area2D 的检测信号 body_entered"  → 检索命中
```

两种改写器（都实现，Router 按场景选）：

```text
指代消解（多轮对话必配）：用最近 2~3 轮上下文补全代词/省略
HyDE（Hypothetical Document Embedding，单轮深问可选）：
  先让 LLM 生成"假想中的完美答案"，用【答案】的向量去检索——
  因为语料库里存的是"答案形态"的文档，query 与 doc 的形态不对称是检索的隐形损耗，
  HyDE 把查询变成"伪文档"，与库内文档同形态，召回提升显著（代价：多一次 LLM 调用）
```

**② 演进**：原始 query 直检（多轮废）→ 指代消解改写（Conversational Query Rewriting，必配基线）→ HyDE（2022 论文）→ 多查询扩展（一变多路检索再 RRF，M10 融合器直接复用）。主线：**检索前处理越贴近文档形态，召回越好**。

**③ 最小案例**：改写提示 + 管道

```python
REWRITE_PROMPT = """基于对话历史，把用户最新输入改写成独立、完整、适合检索的查询。
保留用户原意与技术词，不回答问题，只改写。
历史：{history}
最新输入：{input}
检索查询："""

async def rewrite(self, history: list[Message], q: str) -> str:
    if not self._needs_rewrite(q):           # 无代词/完整句 fast-path 跳过
        return q
    return (await self.llm.complete(REWRITE_PROMPT.format(
        history=self._digest(history, turns=3), input=q))).strip()
```

**④ 易错点**
- `_needs_rewrite` 的 fast-path 很值钱：完整独立句（含具体技术名词、无代词）占比过半，跳过省一次调用与 300ms
- 改写要防"越权补充"——LLM 顺手把问题"回答"了（改写成陈述句），提示里"不回答只改写"要说死
- 语义缓存（M02）的键要用**改写后**的查询，否则"那它的信号呢"每次都是新键、永不命中

### 1.3 路由决策：通道编排的规则 + 模型混合制

**① 原理**

路由 = 意图 + 开关 + 成本信号的函数，输出**通道执行计划**：

```python
@dataclass
class RoutePlan:
    channels: list[Channel]          # 执行哪些通道（可并行）
    mode: str | None                 # 联动 Agent 模式（code_edit→craft）
    budget: dict                     # 每通道预算（检索条数/联网条数）

def decide(intent: Intent, ctx: RoutingContext) -> RoutePlan:
    if intent is CHITCHAT:      return plan([])                          # 空通道直答
    if intent is CODE_EDIT:     return plan([], mode="craft")            # 交 M13，Loop 内自决检索
    if intent is SEARCH:        return plan([WEB], budget={"n": 5})      # 联网优先
    if intent is KNOWLEDGE:
        ch = []
        if ctx.kb_enabled:   ch.append(RAG)
        if ctx.graph_ready:  ch.append(GRAPH)     # 项目图已建才走
        if ctx.multi_hop_hint: ch = [GRAPH, RAG]  # 追问含"哪些/影响/依赖"多跳信号词
        if not ch:           ch = [LLM_DIRECT]    # 全关时明示直答（且提示可能过时）
        return plan(ch)
```

要点：**用户开关是硬约束，意图是软信号**——用户关了知识库，意图再像 knowledge 也不许走 RAG（信任问题），只加提示"本次未启用知识库，答案可能不含你的私有文档"。CODE_EDIT 不预取知识而交 craft 模式自决——检索时机交给 Loop（Agentic 检索），避免"检索了一堆用不上"。

**② 演进**：单通道 → 规则路由（if-else）→ 模型路由（LLM 输出 JSON 计划）→ **Agentic RAG**（检索决策内化进 Agent Loop：模型在推理过程中自主调用检索工具、看结果决定再检索或回答——M14 把 RAG 注册为工具后自然获得）。理解层次：Query Engine 的"检索编排"与 Agent 的"工具自决"是同一问题的两种形态，本项目两者并存：**问答场景走 Engine（省、快），编辑任务走 Agentic（准、稳）**。

**③ 最小案例**：多跳信号词的启发式

```python
MULTI_JUMP = re.compile(r"(哪些|影响|依赖|用到|引用|之间|关系|区别.*和)")
# "删这个信号影响哪些场景" → GRAPH 优先；"Area2D 和 StaticBody2D 区别" → RAG（对比类文档）
```

**④ 易错点**
- 路由决策要可解释：每个 RoutePlan 附 `reason` 字段落 trace——上线后排障与调优全靠它
- 通道并行执行要共享结果去重（同一文档被 RAG 与联网同时返回）
- 灰度新通道（如 GRAPH）先 shadow 模式（执行但不注入，只记录"若启用会命中什么"）

### 1.4 结果整合与注入格式

**① 原理**

多通道结果汇成统一注入块（进 M07 的 RAG 分区）：

```text
<retrieved_context query="Area2D 检测信号" router="RAG+GRAPH" reason="knowledge意图+多跳词">
  [知识库]
  [1] (docs/area2d.md#signals) body_entered 在 monitoring=true 时...
  [2] (docs/collision.md#入门) ...
  [项目图谱]
  推理链：player.tscn/Area2D --LISTENS--> body_entered --HANDLER--> hitbox.gd
  [联网]（未启用）
</retrieved_context>
```

**通道来源标注**让模型能区分"用户项目的事实"（图谱，权威）与"通用文档"（RAG，参考）——两源冲突时以项目事实优先，这个规则写进 system。

**② 演进**：拼接注入 → 带来源结构化注入 → 按通道分权注入。第三步是本节形态。

**③ 最小案例**：整合器（去重与预算）

```python
async def consolidate(self, results: dict[Channel, list[Any]]) -> str:
    seen_docs: set[str] = set(); blocks = []
    for ch in (GRAPH, RAG, WEB):                    # 图谱优先（项目事实）
        items = results.get(ch) or []
        if ch is RAG:
            items = [h for h in items if h.chunk.doc_id not in seen_docs]
            seen_docs.update(h.chunk.doc_id for h in items)
        blocks.append(self._render(ch, items))
    return wrap("retrieved_context", "\n".join(blocks), meta=self.plan.meta)
```

**④ 易错点**
- 注入总预算受 M07 分区钳制（4k），整合器超了要按通道优先级截（GRAPH> RAG > WEB）
- 空结果通道别渲染空标题（"[联网]（未启用）"比空块干净）——模型对空块会尝试"脑补"
- 检索耗时与命中数落 metrics（M21 仪表盘的核心曲线之一）

---

## 2. 接口设计（完整签名）

```python
# query_engine/intent.py
class Intent(Enum):
    CODE_EDIT="code_edit"; KNOWLEDGE="knowledge"; CHITCHAT="chitchat"
    SEARCH="search"; AMBIGUOUS="ambiguous"; UNKNOWN="unknown"
class IntentClassifier:
    async def classify(self, input: str, history: list[Message]) -> Intent: ...

# query_engine/rewriter.py
class QueryRewriter:
    async def rewrite(self, input: str, history: list[Message]) -> str: ...      # 指代消解
    async def hyde(self, query: str) -> str: ...                                 # 伪文档
    def _needs_rewrite(self, input: str) -> bool: ...

# query_engine/router.py
class Channel(Enum): RAG="rag"; GRAPH="graph"; WEB="web"; LLM_DIRECT="llm"
@dataclass
class RoutePlan:
    channels: list[Channel]; mode: str | None
    reason: str; budget: dict[str, int]
    meta: dict                                   # trace 用
class QueryRouter:
    def __init__(self, config: RoutingConfig): ...
    def decide(self, intent: Intent, ctx: RoutingContext) -> RoutePlan: ...

# query_engine/pipeline.py
class QueryEngine:
    def __init__(self, classifier, rewriter, router,
                 rag: HybridRetriever, graph: GraphVectorFusion,
                 web: WebSearchProvider, llm: LLM): ...
    async def process(self, input: str, history: list[Message],
                      ctx: RoutingContext) -> QueryResult: ...
    # QueryResult: intent, rewritten, plan, context_block(str), elapsed_ms
```

## 3. 关键难点参考片段：ambiguous 的消解回路

"那第二个呢？"——无上文无法分类。消解顺序：先做**改写**（补全成独立句），再**二次分类**，仍不明则保守路由：

```python
async def process(self, input, history, ctx):
    intent = await self.classifier.classify(input, history)
    if intent is Intent.AMBIGUOUS:
        rewritten = await self.rewriter.rewrite(input, history)
        intent = await self.classifier.classify(rewritten, history)   # 二次分类
        if intent is Intent.AMBIGUOUS:                                  # 仍模糊→保守
            intent = Intent.KNOWLEDGE
    else:
        rewritten = await self.rewriter.rewrite(input, history)
    plan = self.router.decide(intent, ctx)
    ...
```

为什么难：改写与分类的**循环依赖**（分类要完整句、改写要意图确认必要性）——用"二次分类 + 保守默认"打破环，并把每步决策落 trace，线上才可调试。

## 4. 手敲指引

| 步骤 | 文件 | 做什么 | 验证 |
|---|---|---|---|
| 1 | intent.py | few-shot 分类 + 缓存 | 30 句标注集准确率 >90% |
| 2 | rewriter.py | 指代消解 + fast-path | "那它的信号呢"正确补全 |
| 3 | router.py | 规则矩阵 + reason | 开关组合表驱动测试 |
| 4 | pipeline.py | 全链编排 + 整合 | 多跳问题双通道注入 |
| 5 | 接线 | Loop 前置 process() | trace 里五段决策全可见 |
| 6 | hyde.py | 伪文档改写（可选开关） | A/B 对比召回提升 |

## 5. 测试与验收

```python
async def test_followup_rewritten_before_retrieval():
    # 上文 Area2D，追问"它的信号" → 检索调用参数含 "Area2D"

def test_router_respects_user_switches():
    ctx = RoutingContext(kb_enabled=False)
    plan = router.decide(Intent.KNOWLEDGE, ctx)
    assert Channel.RAG not in plan.channels and "未启用" in plan.reason

async def test_chitchat_zero_retrieval_calls():
    result = await engine.process("谢谢！", history=[], ctx=ctx)
    assert result.plan.channels == [] and mock_rag.call_count == 0
```

**验收 Demo（MI-3 收官）**：
1. "Area2D 怎么检测碰撞？"（knowledge→RAG 带引用）
2. 追问 "那它的信号呢？"（改写后仍命中）
3. "删掉 hitbox 信号影响哪些场景？"（多跳→GRAPH 推理链）
4. "Godot 下个大版本什么时候发？"（search→联网）
5. "帮我加双跳"（code_edit→转 craft 模式，M13 接棒）
五连发 trace 检查：每条的路由 reason、耗时、token 成本符合预期。

## 6. 踩坑记录（留白）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

## 7. 面试拷打

1. 意图分类三种实现方案的三角权衡？为什么主分类器选 LLM few-shot？
2. 为什么检索前必须改写？"形态不对称损耗"怎么理解？
3. HyDE 的原理与代价？什么场景收益最大？
4. 用户开关与意图冲突时（关知识库但问知识）怎么处理？为什么信任优先？
5. code_edit 意图为什么不预取知识而交给 Loop 自决？（预取 vs Agentic 检索）
6. Query Engine 检索编排与 Agent 工具自决的关系？本项目为什么两者并存？
7. ambiguous 的消解回路为什么不能无限递归？保守默认选 knowledge 的理由？
8. 多通道结果整合的优先级与预算怎么定？图谱为什么排第一？
9. shadow 模式灰度新通道是什么？解决什么上线风险？
10. 开放题：为 Query Engine 设计线上评估闭环（路由准确率怎么自动度量？）

## 8. 教程映射与延伸

- 📘 zero2Agent 03 课（与 M02 合为完整 Query Engine）
- 必读：HyDE 论文（Gao et al. 2022，短文精读）；LLMClassifier 相关博客
- 选读：Self-RAG / CRAG（自适应检索的两种范式，与 Agentic 检索对照）
