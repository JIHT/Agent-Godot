"""graphrag/fusion.py —— 图文双引擎融合（M11 §1.4 / §4 步骤 5）

律师办案：案卷（RAG 文档）告诉你"法律条文怎么说"，关系网调查（图谱）
告诉你"你的项目实际怎么连线"——两路并行，结论带证据链。

关键规则：
- 图谱是"你的项目的事实"（权威），文档是"通用知识"（参考）；
- 图空结果 ≠ 无答案：可能是图未建（新项目没跑 full_sync）——降级到
  纯向量并明示（§7 面试 7 三步：区分原因 → 降级执行 → 不阻塞主流程）；
- 图查询 2s 超时同样降级——宁可少一路证据，不可让用户干等。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..rag.citation import CitationFormatter
from ..rag.retrieval import HybridRetriever, RetrievalHit
from .project_graph import ProjectGraphSync

logger = logging.getLogger(__name__)


@dataclass
class GraphPath:
    """一条推理链：节点序列 + 边类型序列（len(edges) == len(nodes)-1）。"""
    nodes: list[str]
    edges: list[str]

    def render(self) -> str:
        """A --LISTENS--> B --ATTACHED_SCRIPT--> c.gd（可解释性的全部）。"""
        if not self.nodes:
            return ""
        out = [str(self.nodes[0])]
        for edge, node in zip(self.edges, self.nodes[1:]):
            out.append(f" --{edge}--> {node}")
        return "".join(out)


@dataclass
class FusionAnswer:
    """融合答案：推理链 + 文档引用 + 降级说明（图降级率进 metrics M21）。"""
    question: str
    answer: str                                   # 渲染好的最终文本
    graph_paths: list[GraphPath] = field(default_factory=list)
    hits: list[RetrievalHit] = field(default_factory=list)
    degraded: bool = False                        # 图路是否缺席
    note: str = ""                                # 降级原因（两种文案）


class GraphVectorFusion:
    """双引擎融合器：图谱 trace + 向量 retrieve 并行 → 合并渲染。"""

    def __init__(self, graph: ProjectGraphSync, hybrid: HybridRetriever,
                 citation: CitationFormatter | None = None,
                 graph_timeout: float = 2.0) -> None:
        self.graph = graph
        self.hybrid = hybrid
        self.citation = citation or CitationFormatter()
        self.graph_timeout = graph_timeout

    # ---------------------------------------------------------- 主路径

    async def answer(self, q: str, project_id: str, top_k: int = 3
                     ) -> FusionAnswer:
        """图文双引擎：两路并行（gather），图路超时/空结果降级不阻塞。"""
        graph_task = asyncio.create_task(self.trace(q, project_id))
        docs_task = asyncio.create_task(self.hybrid.retrieve(q, top_k=top_k))
        try:
            graph_paths = await asyncio.wait_for(graph_task,
                                                 timeout=self.graph_timeout)
        except Exception as e:                            # noqa: BLE001
            # 超时/图未建/连接失败统一降级——宁可少一路证据，不可让用户干等
            logger.warning("图路降级（%s），仅向量回答", type(e).__name__)
            graph_paths = []
        docs = await docs_task

        if not graph_paths:
            return await self._degraded_answer(q, project_id, docs)

        return FusionAnswer(
            question=q,
            answer=(f"推理链：\n" + "\n".join(p.render() for p in graph_paths)
                    + "\n\n参考文档：\n"
                    + self.citation.render_context(docs)),
            graph_paths=graph_paths, hits=docs, degraded=False)

    async def _degraded_answer(self, q: str, project_id: str,
                               docs: list[RetrievalHit]) -> FusionAnswer:
        """降级文案区分两种原因：图未建 vs 建了但查不到（§7 面试 7 ①）。"""
        try:
            built = await self.graph.graph_exists(project_id)
        except Exception:                             # noqa: BLE001
            built = False
        note = ("项目图谱未构建，本次仅用文档回答（建议先 full_sync 建图）"
                if not built
                else "图谱未发现该关系，以下为文档参考")
        return FusionAnswer(
            question=q, degraded=True, note=note, hits=docs,
            answer=f"（{note}）\n\n参考文档：\n"
                   + self.citation.render_context(docs))

    # ---------------------------------------------------------- 图谱路径

    async def trace(self, q: str, project_id: str) -> list[GraphPath]:
        """从问题里定位信号名 → 每个信号跑影响分析 → 推理链。

        信号词典 = 图里项目的全部 DECLARES 信号名；子串命中即视为
        问题所指（教学版启发式；多跳意图判别在 M12 融合决策层）。
        """
        signals = await self.graph.signals_of_project(project_id)
        hit_signals = [s for s in signals if s and s in q]
        paths: list[GraphPath] = []
        for signal in hit_signals:
            impacts = await self.graph.impact_of_signal(project_id, signal)
            for imp in impacts:
                # health_changed <-LISTENS- Main(-tscn) <-ATTACHED_SCRIPT- main.gd
                # 渲染方向取信息流向（信号 → 监听者 → 脚本）：
                paths.append(GraphPath(
                    nodes=[signal,
                           f"{imp.node}（{_short(imp.path)}）",
                           _short(imp.script)],
                    edges=["LISTENS", "ATTACHED_SCRIPT"]))
        return paths


def _short(path: str) -> str:
    """路径缩写：只留文件名（渲染一行放得下）。"""
    return path.rsplit("/", 1)[-1].split("#", 1)[0] if path else path


__all__ = ["FusionAnswer", "GraphPath", "GraphVectorFusion"]
