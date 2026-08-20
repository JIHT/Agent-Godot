# M11 GraphRAG（知识图谱 · 多跳推理 · 图文融合）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 8 · 里程碑 MI-3「知识三件套齐」上半场 |
| 代码落点 | `backend/agent_godot/graphrag/`（4 个文件，见 §0.5） |
| 前置模块 | M10（向量检索复用；图文融合的前提） |
| 手写比例 | 图谱构建/查询/融合 100% 手写；Neo4j 用库（Cypher 手写） |
| 教程映射 | 📙 all-in-rag GraphRAG 章 · 📝笔记知识图谱 · Neo4j 官方入门 |

---

## 0. 本模块在项目中的位置

**大白话**：向量检索（M10）是**图书管理员**——你描述意思，他找出"意思相近"的书页；但问"信号 body_entered 被哪些场景监听？这些场景又依赖哪些脚本？"这种**要沿关系链走两步**的问题，他无能为力——书页之间没有"连线"。知识图谱是**地铁线路图**：站点（实体）+线路（关系），"从 A 站怎么到 B 站"=沿着边走几跳，答案天然带**路径**（推理链），可解释性拉满。

本项目两类图（M00 已定）：

```text
API 知识图谱（离线建）：Godot 类/方法/信号/属性 + 继承与依赖边    ← 问答用
项目结构图（实时建）：本项目 场景↔脚本↔信号连线/资源引用          ← 代码导航用
```

**交付后状态**：多跳问题命中图谱路径并给出可验证的推理链；项目结构图随每次编辑增量更新。

---

## 0.5 ★ 施工文件清单（开工前必看的一页表）

**本模块你一共要新建 5 个文件**：

| # | 新建文件（完整路径） | 职责一句话 | 关键类/函数 | 预估行数 | 手敲步骤(§4) | 依赖 |
|---|---|---|---|---|---|---|
| 1 | `lab/m11/cypher_tour.py` | Neo4j 沙盒练 10 条 Cypher | — | 30 | 步骤 1 | Neo4j 容器 |
| 2 | `graphrag/__init__.py` 等 | 空包 | — | 2 | 步骤 0 | — |
| 3 | `graphrag/cypher.py` | 8 个参数化模板 | `CYPHER_TEMPLATES`、`query()` | 70 | 步骤 2 | neo4j driver |
| 4 | `graphrag/project_graph.py` | 代码→图（解析式）+ 增量同步 | `ProjectGraphSync` | 120 | 步骤 3 | M06 解析器 |
| 5 | `graphrag/graph_builder.py` | 文档→图（LLM 抽取） | `ApiGraphBuilder`、`ApiCatalog` | 150 | 步骤 4 | M02 LLM + M10 docs |
| 6 | `graphrag/fusion.py` | 图文双引擎融合 | `GraphVectorFusion`、`GraphPath` | 60 | 步骤 5 | M10 hybrid |

**依赖链**：`cypher（先会查询语言）→ project_graph（确定性事实先进图）→ graph_builder（LLM 抽取补知识图）→ fusion（双引擎合体）`。

**完成后你拥有**：多跳问题带推理链回答；`graph_query` 注册为工具（M12/M14 后模型可自选）。

---

## 1. 知识点详解（每节五段：定义 → 大白话 → 举例 → 演进 → 易错点）

### 1.1 属性图模型与 Cypher

**① 严格定义**：属性图 = 节点（含属性）+ 关系（有类型、有方向、含属性）。本项目 API 图谱 schema（节点 6 类边 6 类）：

```cypher
// (:Class)-[:INHERITS]->(:Class)            继承：Area2D :INHERITS> CollisionObject2D
// (:Class)-[:HAS_METHOD]->(:Method)         CharacterBody2D 有 move_and_slide()
// (:Class)-[:HAS_SIGNAL]->(:Signal)         Area2D 有 body_entered
// (:Method)-[:PARAM]->(:Param)   (:Method)-[:RETURNS]->(:Type)
// (:Signal)-[:EMITTED_WHEN]->(:Concept)     body_entered → "监测体进入且 monitoring 开启"
// (:Doc)-[:DESCRIBES]->(:Class)             文档节点挂回 RAG 源（图文互链！）
```

Cypher 的**模式匹配**本质：`(a:Area2D)-[:HAS_SIGNAL]->(s)<-[:LISTENS]-(sc:Scene)` 描述"图形状"，引擎找所有匹配子图；多跳=模式里串两个关系一步到位：

```cypher
// "body_entered 被哪些场景监听，这些场景挂了什么脚本？"
MATCH (sig:Signal {name:'body_entered'})<-[:LISTENS]-(node:SceneNode)
      -[:ATTACHED_SCRIPT]->(script:Script)
WHERE node.project = $project
RETURN node.name, script.path
```

**② 大白话**：**地铁线路图查询**。"从 A 站到 B 站怎么走"在地图上=沿着线找路径；Cypher 的 MATCH 就是在说"我要找这样形状的一小段路：从信号站出发，坐 LISTENS 线反向一站到场景站，再换 ATTACHED_SCRIPT 线一站到脚本站"——引擎把所有符合形状的路径都找出来。方向（箭头）是语义的一部分：`HAS_METHOD`（类拥有方法）与反过来写是两条不同的线，全库统一一个方向，查询模式才写得出来。

**③ 举例**：`lab/m11/cypher_tour.py`（官方电影样例库练 10 条——多跳/聚合/最短路径各几条）：

```python
from neo4j import GraphDatabase
driver = GraphDatabase.driver("bolt://localhost:7687", auth=("", ""))
QUERIES = {
 "两跳继承链": "MATCH (c:Class)-[:INHERITS*1..2]->(base) RETURN c.name, base.name LIMIT 5",
 "监听某信号的节点数": "MATCH ()-[l:LISTENS]->(:Signal {name:$s}) RETURN count(l)",
 "未监听任何信号的脚本": "MATCH (s:Script) WHERE NOT (:Signal)<-[:LISTENS]-(())-->(s) RETURN s.path",
}
```

**④ 演进**：RDF 三元组（学术、SPARQL）→ Labeled Property Graph（Neo4j，工业事实标准，Cypher 2011）→ GQL 国际标准（2024，Cypher 直系）。选 Neo4j：Cypher 资料最多、可视化浏览器对调试图谱无敌。

**⑤ 易错点**：
- 无界变长路径 `[:INHERITS*]` 是性能炸弹，生产查询一律 `*1..3` 限深
- 边方向是建模语义，全库统一，混用后模式对不上
- 参数化查询防注入（`$project`），f-string 拼 Cypher 是重罪

### 1.2 图谱构建：从文档到图（抽取流水线）

**① 严格定义**：Godot 官方文档 → LLM 抽取实体与关系 → 三元组清洗 → 入图，四步：实体识别（Class/Method/Signal/Property+标准名）→ 关系抽取（**闭集**六类边+few-shot）→ 清洗对齐（种子字典归一，无命中进人工审核队列）→ MERGE 入图（幂等）。

**② 大白话**：**从散文里整理人物关系表**。把一本小说（文档）交给助理（LLM）："抽出所有人物和他们的关系，但关系只准用这六种：父子/夫妻/上下级…"（闭集）——不限定的话助理会发明"幼年挚友""宿敌"等三百种关系，表就没法用了。抽完还要**户籍核对**（种子字典对齐）：助理写的"王小明、小明、明明"必须是同一个人——对照官方户口本（API 清单）归一成"王小明"，对不上的放进"待核实"堆（uncertain 队列），绝不蒙混入册。

**③ 举例**：抽取提示骨架：

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

**④ 演进**：人工本体（贵、准）→ 规则正则（脆）→ LLM 开放抽取（Microsoft GraphRAG 2024：自由实体+社区摘要，适合无 schema 场景但查询不稳）→ LLM+种子字典+闭集（本项目：领域 schema 明确时最稳）。面试要点：**知道何时用开放（探索型语料）何时用闭集（领域本体清晰）**。

**⑤ 易错点**：
- MERGE 必须带唯一约束（`CREATE CONSTRAINT FOR (c:Class) REQUIRE c.name IS UNIQUE`），否则并发建图产生重复
- LLM 幻觉边比例可达 5%——evidence 字段留审计，抽检回路必须有
- 全库抽取成本高：按章节分批+断点续建（doc_id 进度表）

### 1.3 项目结构图：从代码到图（解析式构建）

**① 严格定义**：与 API 图谱不同，项目结构图**不靠 LLM**——M06 解析器已给出确定性事实，直接翻译成图：`parse_tscn → SceneNode 树+connection 信号线+ExtResource 引用`；`.gd` 轻量 AST → 类/方法/信号声明/preload 引用。增量更新：文件写后删旧子图（`DETACH DELETE`）再重插——文件级原子替换。

**② 大白话**：API 图谱是**从散文里整理关系**（要靠 LLM 阅读）；项目结构图是**照着施工图纸誊关系**（解析器已经把事实算准了，翻译就行）。为什么不用 LLM 画自己的项目图？**确定性的东西不需要概率模型**——解析器给的节点树是 100% 准的，LLM 抄写反而引入 5% 抄错率。这张图使能的查询：变更影响分析（"改这个信号签名影响谁"）、死信号体检（"没有任何脚本监听的信号"）。

**③ 举例**：场景→图翻译器骨架：

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

**④ 演进**：grep 搜调用（文本级）→ LSP 引用查找（精确但无全局视图）→ 图数据库全局关系图（跨文件导航+影响分析+可视化）。三者互补：精确跳转用 LSP 思想，"影响面"问题用图。

**⑤ 易错点**：
- 节点唯一键选 (project, path) 而非 name——不同场景可同名节点
- DETACH DELETE 后事务内重插，中途失败留残图：文件级子图更新包一个事务
- connection 的 `from` 是相对路径（"Hitbox"），映射到节点要按场景内路径解析（M06 的 _abs_path 复用）

### 1.4 图文融合查询（向量 + 图谱双引擎）

**① 严格定义**：问题分三类走不同引擎（融合决策在 M12，本模块提供原语）：事实型→向量；多跳型→图谱；**混合型→两路并行**（向量召回文档段落+图谱查项目信号连线→合并注入，图谱路径渲染成"推理链"文本）。

**② 大白话**：**律师办案**：案卷（文档/RAG）告诉你"法律条文怎么说"，关系网调查（图谱）告诉你"当事人之间实际怎么连线"。疑难案子（混合型）两路都查：条文说"信号需 monitoring=true 才触发"（文档），调查显示"你的 player.tscn 根本没连这条信号"（图谱路径）——**两源合证，结论带证据链**。关键规则：图谱是"你的项目的事实"（权威），文档是"通用知识"（参考），冲突时项目事实优先。

**③ 举例**：双路融合渲染：

```python
async def answer_multi_hop(self, q: str, project: str) -> str:
    path = await self.graph.trace(q, project)         # 图谱路径（Cypher 模板库）
    docs = await self.hybrid.retrieve(q, top_k=3)     # M10 复用
    return (f"推理链：{path.render()}\n\n"
            f"参考文档：\n{self.citation.render_context(docs)}")
# path.render() 示例:
# Area2D(body_entered) --LISTENS--> player.tscn/Hitbox --ATTACHED_SCRIPT--> hitbox.gd
```

**④ 演进**：纯向量 → GraphRAG（微软：图社区摘要做全局问答）→ 图文双引擎路由（当前主流工程形态）→ Agentic 图查询（模型写 Cypher 自主探索——本模块注册 `graph_query` 工具，M12 后模型可自选）。

**⑤ 易错点**：
- 图查询空结果≠无答案：可能是图未覆盖（新项目没建图）——融合层降级到纯向量并告知
- 图谱与文档版本要同步（4.3 图谱配 4.4 文档=灾难），doc 版本号挂图属性
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
CYPHER_TEMPLATES: dict[str, str]      # 8 个参数化模板
def query(driver, template: str, **params) -> list[dict]: ...

# graphrag/fusion.py
@dataclass
class GraphPath: nodes: list[str]; edges: list[str]
    def render(self) -> str: ...      # "A --LISTENS--> B --SCRIPT--> c.gd"
class GraphVectorFusion:
    def __init__(self, graph: ProjectGraphSync, hybrid: HybridRetriever): ...
    async def answer(self, q: str, project_id: str) -> FusionAnswer: ...
```

---

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

---

## 4. 手敲指引（函数级伪代码）

| 步骤 | 文件 | 函数级作用（伪代码） | 验证 |
|---|---|---|---|
| 1 | `lab/m11/cypher_tour.py` | `连接 bolt → 执行 10 条模板查询逐条打印+注释"这条在干嘛"` | 每条结果可解释 |
| 2 | `cypher.py` | `8 个模板：两跳继承/信号监听者/影响分析/死信号/最短依赖路径等，全部参数化+LIMIT 注入` | 表驱动参数测试 |
| 3 | `project_graph.py` | `full_sync：遍历项目文件→M06 解析→scene_to_cypher 语句批→事务执行；upsert_file：MATCH(n{path})DETACH DELETE→重插（文件级原子）；impact_of_signal：模板查询返回边列表` | 样例项目建图后浏览器可视化对 |
| 4 | `graph_builder.py` | `build_from_docs：分批喂文档→EXTRACT_PROMPT 抽三元组→_align 种子对齐（§3）→MERGE 入图→BuildReport；断点续建：doc_id 进度表` | 100 章节抽检准确率 >90% |
| 5 | `fusion.py` | `answer：graph.trace+hybrid.retrieve 并行→§1.4 ③ 渲染；图空结果降级纯向量` | 多跳问题出推理链+引用 |

---

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

**验收 Demo**：导入 lab/m06 样例项目建图 → `ask "如果我删除 hitbox 信号，哪些场景会受影响？"` → 返回推理链路径与受影响文件清单；再问 "Godot 里 CollisionObject2D 和 Area2D 什么关系？" → 图谱两跳继承链+文档引用双呈现。

---

## 6. 踩坑记录（留白自填）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

---

## 7. 面试拷打（附详细参考答案）

**1. 什么问题向量检索答不了而图谱能答？给两个判断标准。**
答：判断标准①**答案是否需要沿关系链串联**（"A 影响哪些 B，B 又关联哪些 C"——答案散落在 N 个 chunk，需要跳转）；②**答案是否是离散实体的精确列举**（"监听 body_entered 的全部场景"——要完备列表，不是"相似段落"）。向量检索按"文本相似"召回，天然走不了关系链——它会给"讲 body_entered 的文档"而不是"连了它的场景清单"。反例（向量主场）："move_and_slide 怎么用"——单文档语义匹配即可。一句话：**关系型问题用图，语义型问题用向量**。

**2. 属性图与 RDF 三元组的取舍？为什么工业界选 LPG？**
答：RDF：一切是 (主,谓,宾) 三元组，W3C 标准、推理机支持（OWL 本体推理）、学术友好；但属性挂载别扭（要 reification）、查询语言 SPARQL 陡峭、生态偏学术。LPG：节点/关系都可直接挂属性，Cypher 的 ASCII 风模式（`(a)-[:R]->(b)`）可视化即查询，工程效率高。工业界选 LPG 的本质：**应用开发要的是"带属性的图+好写的查询"，不是形式化推理**——除非你要做本体推理（医疗术语 SNOMED 那类），LPG 全面占优。GQL 国际标准（2024）也是 LPG 直系，趋势已定。

**3. 开放抽取 vs 闭集抽取，各自适用什么语料？关系类型爆炸为什么致命？**
答：开放（LLM 自由发明实体/关系类型）适用：无预定义本体的探索型语料（新闻、企业情报——你事先不知道有什么实体）；闭集（预定义 N 类边+few-shot）适用：领域本体清晰的语料（Godot API、医疗术语）。关系类型爆炸致命在**查询层**：图谱的价值是"写一个模式匹配所有实例"——300 种关系类型意味着任何问题都可能涉及你没想到的边名，查询写不全、写不对；抽取一致性也崩（同一语义 LLM 今天抽 DEPENDS_ON 明天抽 USES）。微软 GraphRAG 的对策是社区摘要绕开精确查询，但代价是答案粒度粗。

**4. MERGE 与 CREATE 的区别？没有唯一约束会怎样？**
答：CREATE 无条件新建（重复执行=重复节点）；MERGE 是"存在即匹配，不存在才创建"（幂等）。没有唯一约束的 MERGE 在**并发下不原子**：两个事务同时查不到同一节点→各自 CREATE→重复节点（check-then-act 竞态，数据库层的经典问题）。解法：`CREATE CONSTRAINT ... REQUIRE c.name IS UNIQUE`——唯一索引使 MERGE 在匹配时走索引且创建时冲突方失败重试，幂等性才真正成立。建图脚本的 constraint 创建永远放在最前。

**5. 抽取对齐为什么要种子字典？误合并与漏合并哪个更糟？**
答：种子字典（官方 API 清单）是把"LLM 输出的自由文本"钉回"标准名"的锚——没有它，"CharacterBody2d/character body 2d/CB2D"裂成三个节点，图的关系链在裂缝处断裂。误合并 vs 漏合并：**误合并更糟**——漏合并只是少一条边（图不完整，查询结果少），误合并是错连（get_node 并到 get_nodes 上，影响分析给出错误答案——**错误的关系比缺失的关系危害大**，因为用户基于它做决策）。所以对齐规则保守（宁漏勿错），对不上的进 uncertain 队列人工审，不蒙混入图。

**6. 项目结构图为什么不用 LLM 而用解析器？**
答：三个理由：①**确定性**——parse_tscn/轻量 AST 给出的是 100% 准确的事实（节点树/信号连线），LLM 抄写引入 ~5% 抄错率，用概率模型处理确定性数据是纯减分；②**成本**——解析是毫秒级零成本，LLM 逐文件抽取是每次编辑都要花的钱（增量更新场景天天发生）；③**可增量**——文件级原子替换（删旧子图重插）依赖解析输出的结构稳定性，LLM 输出格式抖动会让增量逻辑复杂化。判据推广：**数据已有结构化来源时，永远用解析器；只有非结构化文本（文档/日志）才轮到 LLM 抽取**。

**7. 图文融合时"图空结果"的降级策略？**
答：三步：①**区分原因**——图未建（新项目没跑过 full_sync）vs 建了但查不到（确实无此关系）——查 meta 节点（project 图是否存在）可判；②**降级执行**——未建图/查不到都降级为纯向量检索，但提示文案不同："项目图谱未构建，本次仅用文档回答"（建议建图）vs "图谱未发现该关系，以下为文档参考"；③**不阻塞主流程**——图查询超时（如 2s）也触发降级，宁可答案少一路证据，不可让用户干等。降级事件落 metrics（图降级率是 M21 仪表盘指标）。

**8. 变更影响分析的 Cypher 怎么写？DEPTH 为什么限 3？**
答：`MATCH (sig:Signal {name:$s})<-[:LISTENS]-(n:SceneNode)-[:ATTACHED_SCRIPT]->(sc:Script) RETURN n.name, sc.path`——沿 LISTENS 反向找监听者、再顺 ATTACHED_SCRIPT 找脚本。若要传递影响（改基类信号影响所有子类场景）：`MATCH (c:Class {name:$x})-[:INHERITS*1..3]->(base)`。DEPTH 限 3 的理由：①组合爆炸——变长路径的匹配数随深度指数增长，无界 `*` 在大图上直接超时；②**语义有效性**——影响分析的业务价值集中在 1~3 跳（直接监听者/直接依赖/再一层传递），3 跳外的"影响"噪声远大于信号（用户看的不是全连通图）；③确定性——LIMIT 配合限深保证响应时间可预测。

**9. 微软 GraphRAG 的社区摘要解决什么问题？本项目为什么没采用？**
答：解决**全局性提问**（global question）："这个语料库整体讲了什么主题/趋势？"——传统 RAG 只能答局部问题（top-k 相似 chunk 永远凑不出"全局视角"）。GraphRAG：实体图上跑社区发现（Leiden 算法）→每个社区生成摘要→全局问题问的是社区摘要而非 chunk。本项目没采用的理由：①Godot 文档有清晰的结构目录（本身就是"社区划分"），全局问题用目录页答更准更省；②建库成本高（社区发现+逐社区 LLM 摘要，全库一次性数千次调用）；③本项目的问答场景以局部事实/多跳关系为主，全局摘要需求弱。它是有价值的工具，但不是本项目的形状。

**10. 开放题：图谱+向量+LLM 写 Cypher 三种查询方式的成本/延迟/准确性三角，怎么按场景路由？**
答：三角对比：模板 Cypher（模板库预写+参数化）——延迟 ms 级、零 LLM 成本、准确性=模板覆盖度（覆盖外无能为力）；向量检索——延迟 ~100ms、零 LLM 成本（嵌入）、准确性=语义召回（关系题弱）；LLM 写 Cypher（Text2Cypher）——延迟 1~3s（生成+执行+可能重试）、一次 LLM 成本、准确性=生成正确率（复杂模式可能写错，需沙盒+LIMIT 兜底）。路由策略：**常用高频问题走模板**（覆盖 80% 流量的 8 个模板）；**语义问题走向量**；**模板覆盖外且确认是多跳题**（M12 的多跳信号词）才升级 Text2Cypher，且执行失败自动降级向量+明示。本质：**贵的通道按需启用，便宜的通道默认兜底**——和模型路由（M02）同一个经济学。

---

## 8. 教程映射与延伸

- 📙 all-in-rag GraphRAG 章节
- 必读：Neo4j Cypher 入门（官方 10 节沙盒）；Microsoft GraphRAG 论文/博客（对比理解）
- 选读：Text2Cypher 评测集；GQL 标准
