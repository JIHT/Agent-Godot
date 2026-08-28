"""lab/m11/cypher_tour.py —— Neo4j 沙盒：10 条 Cypher 练习（M11 §1.1 ③ / §4 步骤 1）

前置：本地起 Neo4j 容器（无鉴权沙盒）：
    docker run -d --name neo4j-m11 -p 7687:7687 -p 7474:7474 \
        -e NEO4J_AUTH=none neo4j:5

前 3 条：官方电影样例库（guide 库）练模式匹配/聚合/最短路径——
先在浏览器 http://localhost:7474 玩 :play movie graph 并导入；
后 7 条：本项目图谱 schema（先跑 project_graph 建好 m06 样例项目的图）。

每条都带"这条在干嘛"的中文注释——结果可解释才算练会。
运行：cd backend && uv run python ../lab/m11/cypher_tour.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agent_godot.graphrag.cypher import (Neo4jGraphDriver, query)  # noqa: E402
from agent_godot.graphrag.project_graph import ProjectGraphSync      # noqa: E402

URI = "bolt://localhost:7687"

# 前三条：电影库（:play movie graph 导入后的 schema）
TOUR = [
    ("① 单跳模式匹配：Tom Hanks 演过哪些电影",
     "MATCH (p:Person {name:'Tom Hanks'})-[:ACTED_IN]->(m:Movie) "
     "RETURN m.title LIMIT 10"),
    ("② 两跳链：和 Tom Hanks 同片出演的导演是谁",
     "MATCH (p:Person {name:'Tom Hanks'})-[:ACTED_IN]->(:Movie)"
     "<-[:DIRECTED]-(d:Person) "
     "RETURN DISTINCT d.name LIMIT 10"),
    ("③ 聚合：每人出演电影数排行（count + 排序）",
     "MATCH (p:Person)-[r:ACTED_IN]->() "
     "RETURN p.name, count(r) AS roles ORDER BY roles DESC LIMIT 5"),
    ("④ 变长路径（限深！）：m06 项目的脚本依赖链 1..3 跳",
     "MATCH (a:Script {project:'m06'})-[:DEPENDS_ON*1..3]->(b:Script) "
     "RETURN a.path, b.path LIMIT 5"),
    ("⑤ 两跳继承链：Area2D 之下继承自谁的谁（API 图）",
     "MATCH (c:Class)-[:INHERITS*1..2]->(base:Class) "
     "RETURN c.name, base.name LIMIT 5"),
    ("⑥ 信号监听者：m06 里谁监听 health_changed（一跳反向）",
     "MATCH (sig:Signal {name:'health_changed'})<-[:LISTENS]-(n:SceneNode) "
     "RETURN n.name, n.path"),
    ("⑦ 多跳一步到位：监听者节点又挂了什么脚本（两跳拼一个模式）",
     "MATCH (sig:Signal {name:'health_changed'})<-[:LISTENS]-(n:SceneNode)"
     "-[:ATTACHED_SCRIPT]->(sc:Script) "
     "RETURN n.name, sc.path"),
    ("⑧ 反模式检查：m06 的死信号（声明了没人听）",
     "MATCH (sc:Script {project:'m06'})-[:DECLARES]->(sig:Signal) "
     "WHERE NOT ()-[:LISTENS]->(sig) RETURN sig.name"),
    ("⑨ 最短路径：两个脚本之间隔了几层依赖",
     "MATCH p = shortestPath((a:Script {project:'m06'})-[*..6]-(b:Script)) "
     "RETURN [n IN nodes(p) | n.path] AS hops LIMIT 5"),
    ("⑩ 图谱元信息：图里各类节点各有多少（收尾自检）",
     "MATCH (n) RETURN labels(n)[0] AS kind, count(*) AS n "
     "ORDER BY n DESC"),
]


async def main() -> None:
    driver = Neo4jGraphDriver(URI, auth=None)
    try:
        await driver.ensure_constraints()
        # 先把 lab/m06 样例项目誊进图（没图的话④~⑨都空）
        sample = Path(__file__).resolve().parents[1] / "lab" / "m06" / "sample"
        sync = ProjectGraphSync(driver)
        await sync.full_sync("m06", sample)

        for title, cypher in TOUR:
            print(f"\n=== {title}")
            print(f"    {cypher[:96]}{'…' if len(cypher) > 96 else ''}")
            rows = await query(driver, cypher, limit=10)
            if not rows:
                print("    （空结果——电影库前三条需先在浏览器导入 :play movie graph）")
            for row in rows:
                print(f"    {dict(row)}")
    finally:
        await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
