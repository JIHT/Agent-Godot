"""graphrag：知识图谱 · 多跳推理 · 图文融合（M11）—— 地铁线路图

向量检索（M10）是图书管理员：只会找"意思相近的书页"；问"信号被哪些
场景监听、这些场景又挂了什么脚本"这种要沿关系链走两步的问题，得靠
地铁线路图：站点（实体）+ 线路（关系），答案天然带路径（推理链）。

两类图：
- API 知识图谱（离线建，LLM 抽取+种子对齐）：Godot 类/方法/信号 → 问答用
- 项目结构图（实时建，解析器直译）：场景↔脚本↔信号连线 → 代码导航用

双引擎融合（fusion）：图谱 = 你的项目的事实（权威），文档 = 通用知识
（参考）；图空结果降级纯向量并明示，不阻塞主流程。
"""
from .cypher import (CYPHER_TEMPLATES, QUERY_TEMPLATES, GraphDriver,
                     InMemoryGraphDriver, Neo4jGraphDriver, query)
from .fusion import FusionAnswer, GraphPath, GraphVectorFusion
from .graph_builder import (ApiCatalog, ApiGraphBuilder, BuildReport,
                            EDGE_KINDS, Entity, EXTRACT_PROMPT, NODE_KINDS,
                            Triple)
from .project_graph import ImpactEdge, ProjectGraphSync

__all__ = [
    # cypher（查询语言 + 驱动双实现）
    "CYPHER_TEMPLATES", "QUERY_TEMPLATES", "GraphDriver",
    "InMemoryGraphDriver", "Neo4jGraphDriver", "query",
    # project_graph（代码 → 图，解析式）
    "ImpactEdge", "ProjectGraphSync",
    # graph_builder（文档 → 图，LLM 抽取）
    "ApiCatalog", "ApiGraphBuilder", "BuildReport", "EDGE_KINDS", "Entity",
    "EXTRACT_PROMPT", "NODE_KINDS", "Triple",
    # fusion（图文双引擎）
    "FusionAnswer", "GraphPath", "GraphVectorFusion",
]
