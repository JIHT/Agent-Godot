"""graphrag/cypher.py —— 模板库 + 图驱动双实现（M11 §1.1 / §4 步骤 2）

地铁线路图的**查询语言与售票机**：
- CYPHER_TEMPLATES = 预印好的车票（8 个查询模板 + 写入模板，全部参数化）：
  高频问题走模板——延迟 ms 级、零 LLM 成本、模板覆盖外无能为力（§7 面试 10
  的"便宜的通道默认兜底"）；模型自己写 Cypher（Text2Cypher）是贵通道，M12 再说。
- GraphDriver 协议 = 售票机接口：只认"执行一条参数化 Cypher"这一个动作，
  不看背后是 Neo4j 还是内存实现——测试塞 InMemoryGraphDriver（不继承任何人）
  就能全流程跑通，和 VectorIndex / InMemoryVectorIndex（M10）同一个思路。

铁律（§1.1 易错点）：
- 全部参数化（$project / $signal），f-string 拼 Cypher 是重罪（注入）；
- 变长路径一律 *1..3 限深，无界 * 是性能炸弹；
- 查询模板一律带 LIMIT $limit（响应时间可预测）。
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Protocol

# ---------------------------------------------------------------- 模板库
# 写入模板（MERGE 幂等；唯一键见各模板注释——建图前 constraint 先行 §7 面试 4）

CYPHER_TEMPLATES: dict[str, str] = {
    # ---- 项目结构图（M06 解析器 → 图的确定性翻译） ----
    # 唯一约束: CREATE CONSTRAINT FOR (m:ProjectMeta) REQUIRE m.project IS UNIQUE
    "merge_project_meta":
        "MERGE (m:ProjectMeta {project:$project})",
    # 唯一约束: (project, path) —— 不同场景可同名节点（§1.3 易错点①）
    "merge_scene_node":
        "MERGE (n:SceneNode {project:$project, path:$path}) "
        "SET n.name=$name, n.type=$type",
    "merge_script_node":
        "MERGE (s:Script {project:$project, path:$path}) SET s.name=$name",
    "merge_class_node":
        "MERGE (c:Class {name:$name}) SET c.desc=$desc",
    "merge_child_edge":
        "MATCH (p:SceneNode {project:$project, path:$parent}), "
        "(c:SceneNode {project:$project, path:$child}) "
        "MERGE (p)-[:CHILD]->(c)",
    # LISTENS：监听者节点 → 信号（method/from 挂边——connection 的另一半事实）
    "merge_listens_edge":
        "MATCH (n:SceneNode {project:$project, path:$path}) "
        "MERGE (sig:Signal {name:$signal}) "
        "MERGE (n)-[:LISTENS {method:$method, from_node:$from_node}]->(sig)",
    # 节点挂脚本（影响分析第二跳的边）
    "merge_attached_script_edge":
        "MATCH (n:SceneNode {project:$project, path:$path}), "
        "(s:Script {project:$project, path:$script}) "
        "MERGE (n)-[:ATTACHED_SCRIPT]->(s)",
    # Script 声明信号（死信号体检的起点：有 DECLARES 无 LISTENS = 死信号）
    "merge_script_declares":
        "MATCH (sc:Script {project:$project, path:$path}) "
        "MERGE (sig:Signal {name:$signal}) "
        "MERGE (sc)-[:DECLARES]->(sig)",
    # Script extends 类（项目图与 API 图的第一根"桥"）
    "merge_script_extends":
        "MATCH (sc:Script {project:$project, path:$path}) "
        "MERGE (c:Class {name:$base}) "
        "MERGE (sc)-[:EXTENDS]->(c)",
    # 脚本间依赖（场景实例化传导）：A 的场景实例化了 B 的场景
    "merge_script_dep":
        "MATCH (a:Script {project:$project, path:$from_script}), "
        "(b:Script {project:$project, path:$to_script}) "
        "MERGE (a)-[:DEPENDS_ON]->(b)",
    # ---- API 知识图谱（LLM 抽取 → MERGE 入图） ----
    "merge_has_method":
        "MATCH (c:Class {name:$class}) "
        "MERGE (m:Method {name:$name}) "
        "MERGE (c)-[:HAS_METHOD {evidence:$evidence}]->(m)",
    "merge_has_signal":
        "MATCH (c:Class {name:$class}) "
        "MERGE (s:Signal {name:$name}) "
        "MERGE (c)-[:HAS_SIGNAL {evidence:$evidence}]->(s)",
    "merge_has_property":
        "MATCH (c:Class {name:$class}) "
        "MERGE (p:Property {name:$name}) "
        "MERGE (c)-[:HAS_PROPERTY {evidence:$evidence}]->(p)",
    "merge_inherits_edge":
        "MATCH (c:Class {name:$child}), (b:Class {name:$base}) "
        "MERGE (c)-[:INHERITS]->(b)",
    # Signal → 概念（body_entered → "监测体进入且 monitoring 开启"）
    "merge_emitted_when":
        "MATCH (s:Signal {name:$signal}) "
        "MERGE (co:Concept {name:$concept}) "
        "MERGE (s)-[:EMITTED_WHEN {evidence:$evidence}]->(co)",
    # Doc 节点挂回 RAG 源（图文互链——融合查询的锚点）
    "merge_doc_node":
        "MERGE (d:Doc {doc_id:$doc_id}) SET d.source=$source",
    "merge_describes_edge":
        "MATCH (d:Doc {doc_id:$doc_id}), (c:Class {name:$class}) "
        "MERGE (d)-[:DESCRIBES]->(c)",
    # ---- 文件级原子替换（§1.3：DETACH DELETE 旧子图 → 事务内重插） ----
    "delete_scene_subgraph":
        "MATCH (n:SceneNode {project:$project}) "
        "WHERE n.path STARTS WITH $prefix "
        "DETACH DELETE n",
    "delete_script_node":
        "MATCH (s:Script {project:$project, path:$path}) DETACH DELETE s",
    # ---- 查询模板（8 个高频问题，全部参数化 + LIMIT 注入） ----
    # ① 项目图是否存在（降级判定的第一步：查 meta 节点）
    "project_exists":
        "MATCH (m:ProjectMeta {project:$project}) RETURN count(m) AS n",
    # ② 谁在监听某信号（多跳第一段）
    "signal_listeners":
        "MATCH (sig:Signal {name:$signal})<-[l:LISTENS]-(n:SceneNode) "
        "WHERE n.project=$project "
        "RETURN n.name AS node, n.path AS path, l.method AS method "
        "LIMIT $limit",
    # ③ 变更影响分析：信号 ← 监听场景 → 脚本（两跳一步到位）
    "impact_of_signal":
        "MATCH (sig:Signal {name:$signal})<-[:LISTENS]-(n:SceneNode)"
        "-[:ATTACHED_SCRIPT]->(sc:Script) "
        "WHERE n.project=$project "
        "RETURN n.name AS node, n.path AS path, sc.path AS script, "
        "sc.name AS script_name "
        "LIMIT $limit",
    # ④ 死信号体检：有 DECLARES 无 LISTENS
    "dead_signals":
        "MATCH (sc:Script {project:$project})-[:DECLARES]->(sig:Signal) "
        "WHERE NOT ()-[:LISTENS]->(sig) "
        "RETURN sig.name AS signal LIMIT $limit",
    # ⑤ 两跳继承链（*1..3 限深——DEPTH>3 噪声远大于信号 §7 面试 8）
    "inherits_chain":
        "MATCH (c:Class {name:$name})-[:INHERITS*1..3]->(b:Class) "
        "RETURN c.name AS child, b.name AS base LIMIT $limit",
    # ⑥ 脚本间最短依赖路径（BFS；上限 6 跳防爆图）
    "shortest_script_dep":
        "MATCH p = shortestPath("
        "(a:Script {project:$project, path:$from})"
        "-[:DEPENDS_ON*..6]-"
        "(b:Script {project:$project, path:$to})) "
        "RETURN [n IN nodes(p) | n.path] AS hops",
    # ⑦ 某类的信号清单（文档问答：Area2D 有哪些信号）
    "signals_of_class":
        "MATCH (c:Class {name:$name})-[:HAS_SIGNAL]->(s:Signal) "
        "RETURN s.name AS signal LIMIT $limit",
    # ⑧ 信号触发条件（EMITTED_WHEN → 概念，可解释性的来源）
    "emitted_when":
        "MATCH (s:Signal {name:$name})-[:EMITTED_WHEN]->(co:Concept) "
        "RETURN co.name AS concept, s.name AS signal LIMIT $limit",
    # ⑨ 项目的全部信号名（fusion.trace 的信号词典）
    "all_signals_of_project":
        "MATCH (sc:Script {project:$project})-[:DECLARES]->(s:Signal) "
        "RETURN s.name AS signal LIMIT $limit",
}

# 内存驱动要认识的全部模板名（双实现一致性的验收清单）
QUERY_TEMPLATES = (
    "project_exists", "signal_listeners", "impact_of_signal", "dead_signals",
    "inherits_chain", "shortest_script_dep", "signals_of_class",
    "emitted_when", "all_signals_of_project",
)


async def query(driver: "GraphDriver", template: str, **params: Any) -> list[dict]:
    """执行一条模板查询（template = 模板名或完整 Cypher 文本）。

    模板名优先——参数化防注入的入口收口；传完整文本的能力留给
    lab 沙盒与（未来的）Text2Cypher 通道。
    """
    text = CYPHER_TEMPLATES.get(template, template)
    return await driver.run(text, params)


# ---------------------------------------------------------------- 协议

class GraphDriver(Protocol):
    """图驱动协议：会执行参数化语句(run) / 会跑事务批(run_tx) 即可上岗。

    neo4j.AsyncGraphDatabase 的 driver 不直接满足（那是远程会话模型），
    Neo4jGraphDriver 是它的包装；InMemoryGraphDriver 是测试/离线沙盒实现。
    """

    async def run(self, cypher: str, params: dict | None = None) -> list[dict]: ...
    async def run_tx(self, statements: list[tuple[str, dict]]) -> None: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------- 内存实现

class InMemoryGraphDriver:
    """内存属性图（接口与 Neo4jGraphDriver 同形）。

    不实现通用 Cypher 解释器（那是 Neo4j 的活）——只识别模板库里的
    语句文本，逐条执行**语义等价**的 Python 逻辑。图数据结构：

    - _nodes: [{"label": str, "props": dict}, ...]（下标即节点 id）
    - _edges: [{"src": int, "rel": str, "dst": int, "props": dict}, ...]

    MERGE 幂等性 = (label + 唯一键) 匹配即复用（等价于"唯一约束 + MERGE"
    的组合，§7 面试 4：constraint 先行，MERGE 才原子）。
    """

    def __init__(self) -> None:
        self._nodes: list[dict] = []
        self._edges: list[dict] = []
        self.stats = {"runs": 0}
        # 模板文本 → 语义 handler（构造期绑定，模板库是闭集）
        self._handlers: dict[str, Any] = {
            CYPHER_TEMPLATES[k]: getattr(self, f"_h_{k}")
            for k in CYPHER_TEMPLATES
            if hasattr(self, f"_h_{k}")
        }

    # ---------- 协议方法 ----------

    async def run(self, cypher: str, params: dict | None = None) -> list[dict]:
        self.stats["runs"] += 1
        handler = self._handlers.get(cypher.strip())
        if handler is None:
            raise ValueError(
                f"InMemoryGraphDriver 只识别模板库语句（未知: {cypher[:60]!r}…）；"
                f"任意 Cypher 请用 Neo4jGraphDriver")
        return handler(params or {}) or []

    async def run_tx(self, statements: list[tuple[str, dict]]) -> None:
        # 教学版无回滚：内存数据本就是易失的，中途异常=调用方重来一次即可
        # （Neo4j 实现里这里是真事务，文件级原子替换依赖它 §1.3 易错点②）
        for cypher, params in statements:
            await self.run(cypher, params)

    async def close(self) -> None:
        return None

    # ---------- MERGE 原语：按 (label, key_props) 唯一定位 ----------

    def _merge_node(self, label: str, keys: dict[str, Any],
                    set_props: dict[str, Any] | None = None) -> int:
        for i, n in enumerate(self._nodes):
            if n["label"] != label:
                continue
            if all(n["props"].get(k) == v for k, v in keys.items()):
                if set_props:
                    n["props"].update(set_props)
                return i
        props = dict(keys)
        if set_props:
            props.update(set_props)
        self._nodes.append({"label": label, "props": props})
        return len(self._nodes) - 1

    def _find(self, label: str, keys: dict[str, Any]) -> int | None:
        for i, n in enumerate(self._nodes):
            if n["label"] == label and all(
                    n["props"].get(k) == v for k, v in keys.items()):
                return i
        return None

    def _merge_edge(self, src: int, rel: str, dst: int,
                    props: dict[str, Any] | None = None) -> None:
        for e in self._edges:
            if e["src"] == src and e["rel"] == rel and e["dst"] == dst:
                if props:
                    e["props"].update(props)
                return
        self._edges.append({"src": src, "rel": rel, "dst": dst,
                            "props": dict(props or {})})

    # ---------- 写入 handler（与模板一一对应） ----------

    def _h_merge_project_meta(self, p: dict) -> None:
        self._merge_node("ProjectMeta", {"project": p["project"]})

    def _h_merge_scene_node(self, p: dict) -> None:
        self._merge_node("SceneNode", {"project": p["project"], "path": p["path"]},
                         {"name": p["name"], "type": p["type"]})

    def _h_merge_script_node(self, p: dict) -> None:
        self._merge_node("Script", {"project": p["project"], "path": p["path"]},
                         {"name": p.get("name", "")})

    def _h_merge_attached_script_edge(self, p: dict) -> None:
        node = self._find("SceneNode", {"project": p["project"],
                                        "path": p["path"]})
        script = self._find("Script", {"project": p["project"],
                                       "path": p["script"]})
        if node is not None and script is not None:
            self._merge_edge(node, "ATTACHED_SCRIPT", script)

    def _h_merge_class_node(self, p: dict) -> None:
        self._merge_node("Class", {"name": p["name"]}, {"desc": p.get("desc", "")})

    def _h_merge_child_edge(self, p: dict) -> None:
        parent = self._find("SceneNode", {"project": p["project"],
                                          "path": p["parent"]})
        child = self._find("SceneNode", {"project": p["project"],
                                         "path": p["child"]})
        if parent is not None and child is not None:
            self._merge_edge(parent, "CHILD", child)

    def _h_merge_listens_edge(self, p: dict) -> None:
        node = self._find("SceneNode", {"project": p["project"],
                                        "path": p["path"]})
        sig = self._merge_node("Signal", {"name": p["signal"]})
        if node is not None:
            self._merge_edge(node, "LISTENS", sig,
                             {"method": p.get("method", ""),
                              "from_node": p.get("from_node", "")})

    def _h_merge_script_declares(self, p: dict) -> None:
        script = self._find("Script", {"project": p["project"],
                                       "path": p["path"]})
        sig = self._merge_node("Signal", {"name": p["signal"]})
        if script is not None:
            self._merge_edge(script, "DECLARES", sig)

    def _h_merge_script_extends(self, p: dict) -> None:
        script = self._find("Script", {"project": p["project"],
                                       "path": p["path"]})
        cls = self._merge_node("Class", {"name": p["base"]})
        if script is not None:
            self._merge_edge(script, "EXTENDS", cls)

    def _h_merge_script_dep(self, p: dict) -> None:
        a = self._find("Script", {"project": p["project"],
                                  "path": p["from_script"]})
        b = self._find("Script", {"project": p["project"],
                                  "path": p["to_script"]})
        if a is not None and b is not None and a != b:
            self._merge_edge(a, "DEPENDS_ON", b)

    def _h_merge_has_method(self, p: dict) -> None:
        cls = self._find("Class", {"name": p["class"]})
        if cls is None:
            return                          # 对齐失败/端点缺失 → 边不入图
        m = self._merge_node("Method", {"name": p["name"]})
        self._merge_edge(cls, "HAS_METHOD", m, {"evidence": p.get("evidence", "")})

    def _h_merge_has_signal(self, p: dict) -> None:
        cls = self._find("Class", {"name": p["class"]})
        if cls is None:
            return
        s = self._merge_node("Signal", {"name": p["name"]})
        self._merge_edge(cls, "HAS_SIGNAL", s, {"evidence": p.get("evidence", "")})

    def _h_merge_has_property(self, p: dict) -> None:
        cls = self._find("Class", {"name": p["class"]})
        if cls is None:
            return
        prop = self._merge_node("Property", {"name": p["name"]})
        self._merge_edge(cls, "HAS_PROPERTY", prop,
                         {"evidence": p.get("evidence", "")})

    def _h_merge_inherits_edge(self, p: dict) -> None:
        child = self._find("Class", {"name": p["child"]})
        base = self._find("Class", {"name": p["base"]})
        if child is not None and base is not None:
            self._merge_edge(child, "INHERITS", base)

    def _h_merge_emitted_when(self, p: dict) -> None:
        sig = self._find("Signal", {"name": p["signal"]})
        if sig is None:
            return
        co = self._merge_node("Concept", {"name": p["concept"]})
        self._merge_edge(sig, "EMITTED_WHEN", co,
                         {"evidence": p.get("evidence", "")})

    def _h_merge_doc_node(self, p: dict) -> None:
        self._merge_node("Doc", {"doc_id": p["doc_id"]},
                         {"source": p.get("source", "")})

    def _h_merge_describes_edge(self, p: dict) -> None:
        doc = self._find("Doc", {"doc_id": p["doc_id"]})
        cls = self._find("Class", {"name": p["class"]})
        if doc is not None and cls is not None:
            self._merge_edge(doc, "DESCRIBES", cls)

    def _h_delete_scene_subgraph(self, p: dict) -> None:
        prefix = p["prefix"]
        doomed = {i for i, n in enumerate(self._nodes)
                  if n["label"] == "SceneNode"
                  and n["props"].get("project") == p["project"]
                  and n["props"].get("path", "").startswith(prefix)}
        self._remove_nodes(doomed)

    def _h_delete_script_node(self, p: dict) -> None:
        doomed = {i for i, n in enumerate(self._nodes)
                  if n["label"] == "Script"
                  and n["props"].get("project") == p["project"]
                  and n["props"].get("path") == p["path"]}
        self._remove_nodes(doomed)

    def _remove_nodes(self, doomed: set[int]) -> None:
        """DETACH DELETE 语义：删节点 + 删所有引用它的边，重排下标。"""
        if not doomed:
            return
        remap = {old: new for new, old in enumerate(
            i for i in range(len(self._nodes)) if i not in doomed)}
        self._nodes = [n for i, n in enumerate(self._nodes) if i not in doomed]
        self._edges = [{"src": remap[e["src"]], "rel": e["rel"],
                        "dst": remap[e["dst"]], "props": e["props"]}
                       for e in self._edges
                       if e["src"] not in doomed and e["dst"] not in doomed]

    # ---------- 查询 handler（字段别名与 Neo4j RETURN 对齐） ----------

    def _h_project_exists(self, p: dict) -> list[dict]:
        n = sum(1 for x in self._nodes
                if x["label"] == "ProjectMeta"
                and x["props"].get("project") == p["project"])
        return [{"n": n}]

    def _h_signal_listeners(self, p: dict) -> list[dict]:
        out: list[dict] = []
        for e in self._edges:
            if e["rel"] != "LISTENS":
                continue
            src, dst = self._nodes[e["src"]], self._nodes[e["dst"]]
            if (dst["label"] == "Signal"
                    and dst["props"].get("name") == p["signal"]
                    and src["props"].get("project") == p["project"]):
                out.append({"node": src["props"].get("name"),
                            "path": src["props"].get("path"),
                            "method": e["props"].get("method", "")})
        return out[:p.get("limit", 100)]

    def _h_impact_of_signal(self, p: dict) -> list[dict]:
        listeners = self._h_signal_listeners(p)
        out = []
        for row in listeners:
            node_idx = self._find("SceneNode", {"project": p["project"],
                                                "path": row["path"]})
            if node_idx is None:
                continue
            for e in self._edges:
                if e["rel"] == "ATTACHED_SCRIPT" and e["src"] == node_idx:
                    script = self._nodes[e["dst"]]
                    out.append({**row, "script": script["props"].get("path"),
                                "script_name": script["props"].get("name", "")})
        return out[:p.get("limit", 100)]

    def _h_dead_signals(self, p: dict) -> list[dict]:
        listened = {self._nodes[e["dst"]]["props"].get("name")
                    for e in self._edges if e["rel"] == "LISTENS"}
        out = []
        for e in self._edges:
            if e["rel"] != "DECLARES":
                continue
            script = self._nodes[e["src"]]
            sig = self._nodes[e["dst"]]
            if (script["props"].get("project") == p["project"]
                    and sig["props"].get("name") not in listened):
                out.append({"signal": sig["props"].get("name")})
        return out[:p.get("limit", 100)]

    def _h_inherits_chain(self, p: dict) -> list[dict]:
        name_index = {n["props"].get("name"): i for i, n in enumerate(self._nodes)
                      if n["label"] == "Class"}
        start = name_index.get(p["name"])
        if start is None:
            return []
        # BFS 1..3 跳（与 *1..3 语义一致）
        seen: dict[int, int] = {}
        queue = deque([(start, 0)])
        while queue:
            cur, depth = queue.popleft()
            if depth >= 3:
                continue
            for e in self._edges:
                if e["rel"] == "INHERITS" and e["src"] == cur:
                    nxt = e["dst"]
                    if nxt not in seen:
                        seen[nxt] = depth + 1
                        queue.append((nxt, depth + 1))
        return [{"child": p["name"], "base": self._nodes[i]["props"].get("name")}
                for i in sorted(seen)][:p.get("limit", 100)]

    def _h_shortest_script_dep(self, p: dict) -> list[dict]:
        a = self._find("Script", {"project": p["project"], "path": p["from"]})
        b = self._find("Script", {"project": p["project"], "path": p["to"]})
        if a is None or b is None:
            return []
        # 无向 BFS（-[:DEPENDS_ON*..6]--，双向可达）
        prev: dict[int, int | None] = {a: None}
        queue = deque([a])
        while queue:
            cur = queue.popleft()
            if cur == b:
                break
            for e in self._edges:
                if e["rel"] != "DEPENDS_ON":
                    continue
                for x, y in ((e["src"], e["dst"]), (e["dst"], e["src"])):
                    if x == cur and y not in prev:
                        prev[y] = x
                        queue.append(y)
        if b not in prev:
            return []
        path, cur = [], b
        while cur is not None:
            path.append(self._nodes[cur]["props"].get("path"))
            cur = prev[cur]
        return [{"hops": list(reversed(path))}]

    def _h_signals_of_class(self, p: dict) -> list[dict]:
        cls = self._find("Class", {"name": p["name"]})
        if cls is None:
            return []
        return [{"signal": self._nodes[e["dst"]]["props"].get("name")}
                for e in self._edges
                if e["rel"] == "HAS_SIGNAL" and e["src"] == cls][:p.get("limit", 100)]

    def _h_emitted_when(self, p: dict) -> list[dict]:
        sig = self._find("Signal", {"name": p["name"]})
        if sig is None:
            return []
        return [{"concept": self._nodes[e["dst"]]["props"].get("name"),
                 "signal": p["name"]}
                for e in self._edges
                if e["rel"] == "EMITTED_WHEN" and e["src"] == sig][:p.get("limit", 100)]

    def _h_all_signals_of_project(self, p: dict) -> list[dict]:
        out, seen = [], set()
        for e in self._edges:
            if e["rel"] != "DECLARES":
                continue
            script = self._nodes[e["src"]]
            if script["props"].get("project") != p["project"]:
                continue
            name = self._nodes[e["dst"]]["props"].get("name")
            if name not in seen:
                seen.add(name)
                out.append({"signal": name})
        return out[:p.get("limit", 100)]

    # ---------- 测试辅助（断言用，非协议成员） ----------

    def count(self, label: str, **match: Any) -> int:
        return sum(1 for n in self._nodes
                   if n["label"] == label
                   and all(n["props"].get(k) == v for k, v in match.items()))

    def count_edges(self, rel: str) -> int:
        return sum(1 for e in self._edges if e["rel"] == rel)


# ---------------------------------------------------------------- Neo4j 实现

class Neo4jGraphDriver:
    """Neo4j 真驱动包装（懒 import：没装 neo4j 包不拖垮 graphrag 包加载）。

    启动约束脚本在 ensure_constraints——MERGE 的幂等性以唯一约束为前提
    （§7 面试 4：并发 check-then-act 竞态的唯一解），constraint 永远先行。
    """

    CONSTRAINTS = (
        "CREATE CONSTRAINT project_meta_id IF NOT EXISTS "
        "FOR (m:ProjectMeta) REQUIRE m.project IS UNIQUE",
        "CREATE CONSTRAINT scene_node_id IF NOT EXISTS "
        "FOR (n:SceneNode) REQUIRE (n.project, n.path) IS UNIQUE",
        "CREATE CONSTRAINT script_id IF NOT EXISTS "
        "FOR (s:Script) REQUIRE (s.project, s.path) IS UNIQUE",
        "CREATE CONSTRAINT class_id IF NOT EXISTS "
        "FOR (c:Class) REQUIRE c.name IS UNIQUE",
        "CREATE CONSTRAINT signal_id IF NOT EXISTS "
        "FOR (s:Signal) REQUIRE s.name IS UNIQUE",
        "CREATE CONSTRAINT method_id IF NOT EXISTS "
        "FOR (m:Method) REQUIRE m.name IS UNIQUE",
        "CREATE CONSTRAINT property_id IF NOT EXISTS "
        "FOR (p:Property) REQUIRE p.name IS UNIQUE",
        "CREATE CONSTRAINT concept_id IF NOT EXISTS "
        "FOR (co:Concept) REQUIRE co.name IS UNIQUE",
        "CREATE CONSTRAINT doc_id IF NOT EXISTS "
        "FOR (d:Doc) REQUIRE d.doc_id IS UNIQUE",
    )

    def __init__(self, uri: str = "bolt://localhost:7687",
                 auth: tuple[str, str] | None = None) -> None:
        from neo4j import AsyncGraphDatabase        # 懒 import（pypdf 同款）
        self._driver = AsyncGraphDatabase.driver(uri, auth=auth)

    async def ensure_constraints(self) -> None:
        for cypher in self.CONSTRAINTS:
            await self.run(cypher)

    async def run(self, cypher: str, params: dict | None = None) -> list[dict]:
        async with self._driver.session() as session:
            result = await session.run(cypher, params or {})
            return [dict(record) async for record in result]

    async def run_tx(self, statements: list[tuple[str, dict]]) -> None:
        """事务批：文件级原子替换的载体（删旧 + 重插同事务，§1.3 易错点②）。"""
        async with self._driver.session() as session:
            async def work(tx) -> None:
                for cypher, params in statements:
                    await tx.run(cypher, params)
            await session.execute_write(work)

    async def close(self) -> None:
        await self._driver.close()


__all__ = ["CYPHER_TEMPLATES", "QUERY_TEMPLATES", "GraphDriver",
           "InMemoryGraphDriver", "Neo4jGraphDriver", "query"]
