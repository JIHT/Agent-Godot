# M11 GraphRAG（知识图谱 · 多跳推理 · 图文融合）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 8 · 里程碑 MI-3「知识三件套齐」上半场 |
| 代码落点 | `backend/agent_godot/graphrag/`（graph_builder/project_graph/cypher/fusion） |
| 前置模块 | M10（向量检索复用；图文融合的前提） |
| 手写比例 | 图谱构建/查询/融合 100% 手写；Neo4j 用库（Cypher 手写） |
| 教程映射 | 📙 all-in-rag GraphRAG 章 · 📝笔记知识图谱 · Neo4j 官方入门 |

---

## 0. 本模块在项目中的位置

向量检索的盲区：**多跳问题**。"信号 `body_entered` 被哪些场景监听？这些场景又依赖哪些脚本？"——答案散落在 N 个文档/chunk 里，需要沿着"信号→场景→脚本"的边走两跳。向量检索按"文本相似"召回，天然走不了关系链；图谱按"边"导航，正是多跳题的解。

本项目两类图（M00 已定）：

```text
API 知识图谱（离线建）：Godot 类/方法/信号/属性 + 继承与依赖边    ← 问答用
项目结构图（实时建）：本项目 场景↔脚本↔信号连线/资源引用          ← 代码导航用
```

**交付后状态**：多跳问题命中图谱路径并给出可验证的推理链；项目结构图随每次编辑增量更新。

---

## 1. 知识点详解

### 1.1 属性图模型与 Cypher

**① 原理**

属性图 = 节点（含属性）+ 关系（有类型、有方向、含属性）：

```cypher
// 本项目的 API 图谱 schema（节点4类 边6类）
// (:Class)-[:INHERITS]->(:Class)            继承：Area2D :INHERITS> CollisionObject2D
// (:Class)-[:HAS_METHOD]->(:Method)         CharacterBody2D 有 move_and_slide()
// (:Class)-[:HAS_SIGNAL]->(:Signal)         Area2D 有 body_entered
// (:Method)-[:PARAM]->(:Param)
// (:Method)-[:RETURNS]->(:Type)
// (:Signal)-[:EMITTED_WHEN]->(:Concept)     body_entered → "监测体进入且 monitoring 开启"
// (:Doc)-[:DESCRIBES]->(:Class)             文档节点挂回 RAG 源（图文互链！）
```

Cypher 查询的**模式匹配**本质：`(a:Area2D)-[:HAS_SIGNAL]->(s)<-[:LISTENS]-(sc:Scene {project:$p})` 描述"图形状"，引擎找所有匹配子图。多跳 = 模式里串两个关系，一步到位：

```cypher
// "body_entered 被哪些场景监听，这些场景挂了什么脚本？"
MATCH (sig:Signal {name:'body_entered'})<-[:LISTENS]-(node:SceneNode)
      -[:ATTACHED_SCRIPT]->(script:Script)
WHERE node.project = $project
RETURN node.name, script.path
```

**② 演进**：RDF 三元组（学术、SPARQL）→ Labeled Property Graph（Neo4j，工业事实标准，Cypher 2011）→ GQL 国际标准（2024，Cypher 直系）。选 Neo4j：Cypher 教学资料最多、可视化浏览器对调试图谱无敌。

**③ 最小案例** `lab/m11/cypher_tour.py`（用官方电影样例库练 10 条查询——多跳/聚合/最短路径各来几条）：

```python
from neo4j import GraphDatabase
driver = GraphDatabase.driver("bolt://localhost:7687", auth=("", ""))
QUERIES = {
 "两跳继承链": "MATCH (c:Class)-[:INHERITS*1..2]->(base) RETURN c.name, base.name LIMIT 5",
 "监听某信号的节点数": "MATCH ()-[l:LISTENS]->(:Signal {name:$s}) RETURN count(l)",
 "未监听任何信号的脚本": "MATCH (s:Script) WHERE NOT (:Signal)<-[:LISTENS]-(())-->(s) RETURN s.path",
}
```

**④ 易错点**
- 无界变长路径 `[:INHERITS*]` 是性能炸弹，生产查询一律 `*1..3` 限深
- 边的方向是建模语义（`HAS_METHOD` vs `BELONGS_TO` 二选一，全库统一），混用后查询模式对不上
- Cypher 参数化查询防注入（`$project`），f-string 拼 Cypher 是重罪

### 1.2 图谱构建：从非结构化文档到图（抽取流水线）

**① 原理**

Godot 官方文档（M10 已解析）→ LLM 抽取实体与关系 → 三元组清洗 → 入图：

```text
Step1 实体识别：章节文档喂给 LLM，提示抽 Class/Method/Signal/Property + 标准名
Step2 关系抽取：抽 INHERITS/EMITTED_WHEN/DESCRIBES 等六类边（限定闭集，防关系类型爆炸）
Step3 清洗对齐：LLM 输出的 "CharacterBody2d"/"character body 2d" 归一 → 官方 API 清单（种子字典）
        冲突丢弃率与置信度入库；无种子命中 → 人工审核队列
Step4 入图：MERGE（幂等，按 (name, type) 唯一键）——重复构建不产生重复节点
```

**闭集关系**是稳定性关键：开放抽取（让 LLM 自由发明关系名）产出的图像薛定谔的猫——关系类型 300 种，查询模式写不了。限定 6 类边 + 每类给 3 个 few-shot，抽取一致性质变。

**② 演进**：人工本体（贵、准）→ 规则正则抽取（脆）→ LLM 开放抽取（Microsoft GraphRAG 2024：自由实体+社区摘要，适合无 schema 场景但查询不稳）→ LLM+种子字典+闭集（本项目：领域 schema 明确时最稳）。面试要点：**知道何时用开放（探索型语料）何时用闭集（领域本体清晰）**。

**③ 最小案例**：抽取提示（骨架）

```python
EXTRACT_PROMPT = """从 Godot 文档片段抽取实体与关系，只允许以下类型：
节点: Class|Method|Signal|Property|Concept|Doc
边: INHERITS|HAS_METHOD|HAS_SIGNAL|HAS_PROPERTY|EMITTED_WHEN|DESCRIBES
规则:
- 名称用官方标准名（对照给出的 API 清单），无法对齐的标记 "uncertain": true
- 每条边给 evidence（原文句子）
输出 JSON: {{"nodes":[...], "edges":[...]}}
片段：{text}"""
```

**④ 易错点**
- MERGE 必须带唯一约束（`CREATE CONSTRAINT FOR (c:Class) REQUIRE c.name IS UNIQUE`），否则并发建图产生重复
- LLM 抽取的"幻觉边"（文档没说的继承关系）比例可达 5%——evidence 字段留审计，抽检回路必须有
- 一次全库抽取成本高：按章节分批 + 断点续建（doc_id 进度表）

### 1.3 项目结构图：从代码到图（解析式构建）

**① 原理**

与 API 图谱不同，项目结构图**不靠 LLM**——M06 的解析器已经给出确定性事实，直接翻译成图：

```text
parse_tscn → SceneNode 树 + connection 信号线 + ExtResource 引用
.gd 轻量 AST → 类定义/方法/信号声明/preload 引用
映射：:SceneNode{project,name,type,path} :Script{project,path,class}
      (:SceneNode)-[:CHILD]->(:SceneNode)
      (:SceneNode)-[:ATTACHED_SCRIPT]->(:Script)
      (:SceneNode)-[:LISTENS]->(:Signal{name})      来自 connection
      (:Script)-[:PRELOADS]->(:Resource)
```

增量更新：每次文件写（M06 工具）后，删该文件的旧子图（`MATCH (n {path:$p}) DETACH DELETE n`）再重插——文件级原子替换。这张图使能的查询（也是工具化的候选）：

```cypher
-- "改这个信号签名会影响谁？"（变更影响分析）
MATCH (sig:Signal {name:$s})<-[:LISTENS]-(n)-[:ATTACHED_SCRIPT]->(sc)
RETURN n.name, sc.path
-- "没有任何脚本监听的信号"（死信号体检）
```

**② 演进**：靠 grep 搜调用（文本级）→ LSP 引用查找（精确但无全局视图）→ 图数据库全局关系图（跨文件导航+影响分析+可视化）。三者互补：精确跳转用 LSP 思想，"影响面"问题用图。

**③ 最小案例**：场景→图翻译器骨架

```python
def scene_to_cypher(sf: SceneFile, project: str) -> list[str]:
    stmts = []
    for n in sf.nodes:
        stmts.append(f"""MERGE (n:SceneNode {{project:$p, path:$path, name:$name}})
                        SET n.type=$type""".replace(...))       # 参数化省略
        if n.parent and n.parent != ".":
            stmts.append("MATCH (p:SceneNode {name:$parent}), (n {name:$name}) MERGE (p)-[:CHILD]->(n)")
    for c in sf.connections:
        stmts.append("MATCH (n {name:$from}) MERGE (s:Signal {name:$sig}) MERGE (n)-[:LISTENS]->(s)")
    return stmts
```

**④ 易错点**
- 节点唯一键选 (project, path) 而非 name——不同场景可同名节点
- DETACH DELETE 后事务内重插，中途失败会留残图：文件级子图更新包一个事务
- connection 的 `from` 是相对路径（"Hitbox"），映射到节点要按场景内路径解析（M06 的 _abs_path 复用）

### 1.4 图文融合查询（向量 + 图谱双引擎）

**① 原理**

用户问题分两类走不同引擎（融合在 M12 Query Engine 决策，本模块提供原语）：

```text
事实型（"move_and_slide 签名"）→ 向量：M10 主场
多跳型（"body_entered 影响哪些脚本"）→ 图谱：Cypher 模式
混合型（"为什么我的碰撞没反应？给可能原因和涉及类"）→ 两路并行：
   向量召回文档段落（故障排查文档）
   图谱查该用户场景的信号连线（LISTENS 边缺失=经典原因）
   → 结果合并注入，图谱路径渲染成"推理链"文本：
     检查发现 player.tscn 的 Area2D 未 LISTENS->body_entered（依据图谱），
     官方文档指出信号需 monitoring=true（依据 [2]）
```

**推理链可视化**是图谱相对向量的独有优势：答案能给出"A→B→C 的路径"而非"相似的段落"——可解释性拉满。

**② 演进**：纯向量 → GraphRAG（微软：图社区摘要做全局问答）→ 图文双引擎路由（当前主流工程形态）→ Agentic 图查询（模型写 Cypher 自主探索，本模块把它注册成工具 `graph_query`，M12 后模型可自选）。

**③ 最小案例**：双路融合渲染

```python
async def answer_multi_hop(self, q: str, project: str) -> str:
    path = await self.graph.trace(q, project)         # 图谱路径（Cypher 模板库）
    docs = await self.hybrid.retrieve(q, top_k=3)     # M10 复用
    return (f"推理链：{path.render()}\n\n"
            f"参考文档：\n{self.citation.render_context(docs)}")
# path.render() 示例:
# Area2D(body_entered) --LISTENS--> player.tscd/Hitbox --ATTACHED_SCRIPT--> hitbox.gd
```

**④ 易错点**
- 图查询空结果≠无答案：可能是图未覆盖（新项目没建图）——融合层要降级到纯向量并告知
- 图谱与文档版本要同步（Godot 4.3 图谱配 4.4 文档答 4.4 问题=灾难），doc 版本号挂图属性
- 模型直接写 Cypher 要防"全图扫描"——工具内强制 depth 上限与 LIMIT 注入

---

## 2. 接口设计（完整签名）

```python
# graphrag/graph_builder.py
@dataclass
class Triple: src: Entity; edge: str; dst: Entity; evidence: str; confidence: float
class ApiGraphBuilder:
    def __init__(self, driver, llm: LLM, seed_api: ApiCatalog): ...
    async def build_from_docs(self, docs: list[ParsedDoc]) -> BuildReport: ...
    # BuildReport: nodes/edges/dropped(未对齐)/uncertain 待审队列

# graphrag/project_graph.py
class ProjectGraphSync:
    def __init__(self, driver): ...
    async def full_sync(self, project_id: str, root: Path) -> int: ...
    async def upsert_file(self, project_id: str, path: Path) -> None: ...
    async def impact_of_signal(self, project_id: str, signal: str) -> list[ImpactEdge]: ...
    async def dead_signals(self, project_id: str) -> list[str]: ...

# graphrag/cypher.py
CYPHER_TEMPLATES: dict[str, str]      # 8 个参数化模板（两跳继承/影响分析/死信号…）
def query(driver, template: str, **params) -> list[dict]: ...

# graphrag/fusion.py
@dataclass
class GraphPath: nodes: list[str]; edges: list[str]
    def render(self) -> str: ...      # "A --LISTENS--> B --SCRIPT--> c.gd"
class GraphVectorFusion:
    def __init__(self, graph: ProjectGraphSync, hybrid: HybridRetriever): ...
    async def answer(self, q: str, project_id: str) -> FusionAnswer: ...
    # FusionAnswer: text + graph_paths + citations
```

## 3. 关键难点参考片段：抽取对齐（种子字典）

LLM 输出归一化是构建质量的命门——"名称漂移"会让同一个类裂成多个节点：

```python
class ApiCatalog:                     # 从 Godot 官方 class_list.xml 构建
    def canonical(self, name: str) -> str | None:
        n = name.strip().replace(" ", "").replace("_", "").lower()
        return self._lookup.get(n)          # 预建规范化索引 "characterbody2d"→"CharacterBody2D"

async def _align(self, entity: Entity) -> Entity | None:
    if entity.kind == "Concept":            # 概念节点免对齐（自由文本）
        return entity
    canon = self.catalog.canonical(entity.name)
    if canon is None:
        self.report.uncertain.append(entity)   # 进人工审核队列，不入图
        return None
    entity.name = canon
    return entity
```

为什么难：规范化的每条规则（去空格/下划线/大小写）都会引入误合并（`get_node` vs `get_nodes`？下划线数量不该抹平）。规则保守 + uncertain 队列兜底，比"全自动"稳一个量级。

## 4. 手敲指引

| 步骤 | 文件 | 做什么 | 验证 |
|---|---|---|---|
| 1 | lab/m11/cypher_tour.py | Neo4j 沙盒练 10 条 Cypher | 每条结果可解释 |
| 2 | cypher.py | 8 个模板 | 表驱动参数测试 |
| 3 | project_graph.py | 场景/脚本→图 | 样例项目建图后浏览器可视化对 |
| 4 | 增量同步 | upsert_file | 改文件后图局部更新 |
| 5 | graph_builder.py | LLM 抽取+对齐 | 100 章节抽检准确率 >90% |
| 6 | fusion.py | 双路融合渲染 | 多跳问题出推理链+引用 |

## 5. 测试与验收

```python
async def test_project_graph_matches_tscn():
    # 全量建图后节点数 = 解析器节点总数；LISTENS 边数 = connection 总数

async def test_upsert_file_replaces_subgraph():
    # 改 player.tscn 后：旧子图消失，新子图节点数正确（无重复残留）

async def test_multi_hop_answer_shows_chain():
    ans = await fusion.answer("body_entered 影响哪些脚本", project)
    assert ans.graph_paths and "--LISTENS-->" in ans.graph_paths[0].render()
```

**验收 Demo**：导入 lab/m06 样例项目建图 → `ask "如果我删除 hitbox 信号，哪些场景会受影响？"` → 返回推理链路径与受影响文件清单；再问 "Godot 里 CollisionObject2D 和 Area2D 什么关系？" → 图谱两跳继承链 + 文档引用双呈现。

## 6. 踩坑记录（留白）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

## 7. 面试拷打

1. 什么问题向量检索答不了而图谱能答？给两个判断标准；
2. 属性图与 RDF 三元组的取舍？为什么工业界选 LPG？
3. 开放抽取 vs 闭集抽取，各自适用什么语料？关系类型爆炸为什么致命？
4. MERGE 与 CREATE 的区别？没有唯一约束会怎样？
5. 抽取对齐为什么要种子字典？误合并与漏合并哪个更糟？
6. 项目结构图为什么不用 LLM 而用解析器？
7. 图文融合时"图空结果"的降级策略？
8. 变更影响分析（impact analysis）的 Cypher 怎么写？DEPTH 为什么限 3？
9. 微软 GraphRAG 的社区摘要解决什么问题（全局性提问）？本项目为什么没采用？
10. 开放题：图谱+向量+LLM 写 Cypher 三种查询方式的成本/延迟/准确性三角，怎么按场景路由？

## 8. 教程映射与延伸

- 📙 all-in-rag GraphRAG 章节
- 必读：Neo4j Cypher 入门（官方 10 节沙盒）；Microsoft GraphRAG 论文/博客（对比理解）
- 选读：Text2Cypher（模型生成查询的评测集）；GQL 标准
