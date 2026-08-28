"""graphrag/graph_builder.py —— API 知识图谱：文档→图（LLM 抽取）
（M11 §1.2 / §3 / §4 步骤 4）

从散文里整理人物关系表：把文档片段交给 LLM，但关系只准用闭集六类边
（不限定的话 LLM 会发明三百种关系，查询层直接崩——§7 面试 3）；
抽完做**户籍核对**（种子字典对齐）：名称漂移会让同一个类裂成多个节点，
对不上的进 uncertain 队列人工审，绝不蒙混入图（宁漏勿错，§3）。

四步流水线：实体识别 → 关系抽取（闭集+few-shot）→ 清洗对齐 → MERGE 入图
（幂等，跑两遍图不变）。断点续建：doc_id 进度表，全库分批抽取的账本。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..core.llm import LLMRequest, Message
from ..rag.parsers import ParsedDoc
from .cypher import CYPHER_TEMPLATES, query

logger = logging.getLogger(__name__)

# 闭集（§1.2 ②：关系只准用这几种，抽出来的才查询得动）
NODE_KINDS = ("Class", "Method", "Signal", "Property", "Concept", "Doc")
EDGE_KINDS = ("INHERITS", "HAS_METHOD", "HAS_SIGNAL", "HAS_PROPERTY",
              "EMITTED_WHEN", "DESCRIBES")

EXTRACT_PROMPT = """从 Godot 文档片段抽取实体与关系，只允许以下类型：
节点: Class|Method|Signal|Property|Concept|Doc
边: INHERITS|HAS_METHOD|HAS_SIGNAL|HAS_PROPERTY|EMITTED_WHEN|DESCRIBES
规则:
- 名称用官方标准名（对照给出的 API 清单），无法对齐的标记 "uncertain": true
- 每条边给 evidence（原文句子）
- INHERITS/HAS_* 的起点必须是 Class；EMITTED_WHEN 起点是 Signal；DESCRIBES 起点是 Doc
输出 JSON: {{"nodes": [{{"name": str, "kind": str, "uncertain": bool}}], "edges": [{{"src": str, "edge": str, "dst": str, "evidence": str}}]}}
可用 API 清单（子集）: {api}
文档片段：
{text}"""

# LLM 输出可能裹 ```json ... ```（教科书式坏习惯，容忍解析）
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


@dataclass
class Entity:
    """图谱节点候选：kind ∈ NODE_KINDS，uncertain = 未对齐待审。"""
    name: str
    kind: str
    uncertain: bool = False
    evidence: str = ""


@dataclass
class Triple:
    """图谱边候选：src --edge--> dst + 审计三件套（evidence/confidence）。"""
    src: str
    edge: str
    dst: str
    evidence: str = ""
    confidence: float = 1.0


@dataclass
class BuildReport:
    """建图报告：入图量 + 丢弃量 + 待审队列（抽检回路的数据源）。"""
    nodes: int = 0                      # 入图节点数
    edges: int = 0                      # 入图边数
    dropped: list[str] = field(default_factory=list)      # 端点未对齐被丢的边
    uncertain: list[Entity] = field(default_factory=list)  # 人工审核队列
    docs_done: list[str] = field(default_factory=list)    # 断点续建账本


class ApiCatalog:
    """种子字典（§3）：官方 API 清单 = 把 LLM 自由文本钉回标准名的锚。

    canonical("character body 2d") → "CharacterBody2D"；
    对不上返回 None（进 uncertain 队列，宁漏勿错）。
    """

    def __init__(self, names: list[str] | tuple[str, ...] | set[str]) -> None:
        # 规范化索引：去空格/下划线 + 转小写（规则保守——只抹"排版差异"，
        # 不抹语义差异：get_node 与 get_nodes 下划线数量不同不会被归一）
        self._lookup: dict[str, str] = {}
        for name in names:
            key = self._norm(name)
            if key and key not in self._lookup:
                self._lookup[key] = name

    @staticmethod
    def _norm(name: str) -> str:
        return name.strip().replace(" ", "").replace("_", "").lower()

    def canonical(self, name: str) -> str | None:
        return self._lookup.get(self._norm(name))

    def __len__(self) -> int:
        return len(self._lookup)

    @classmethod
    def from_class_list(cls, path: str | Path) -> "ApiCatalog":
        """从 Godot 官方 class_list.xml 构建（懒 import，离线教学版可不装）。"""
        import xml.etree.ElementTree as ET
        root = ET.parse(path).getroot()
        return cls([el.get("name", "") for el in root.iter("class")])


class ApiGraphBuilder:
    """文档 → API 知识图谱（LLM 抽取 + 种子对齐 + MERGE 入图）。"""

    def __init__(self, driver, llm, seed_api: ApiCatalog,
                 model: str = "graph-extractor", batch_chars: int = 3000) -> None:
        self.driver = driver
        self.llm = llm
        self.catalog = seed_api
        self.model = model
        self.batch_chars = batch_chars
        self._done: set[str] = set()       # 断点续建：doc_id 进度表

    # ---------------------------------------------------------- 主流程

    async def build_from_docs(self, docs: list[ParsedDoc]) -> BuildReport:
        report = BuildReport()
        for doc in docs:
            if doc.doc_id in self._done:              # 断点续建：跳过已完成
                continue
            for batch in self._batches(doc.text):
                nodes, edges = await self._extract(batch)
                await self._merge(doc=doc, nodes=nodes, edges=edges,
                                  report=report)
            # Doc 节点：图文互链的锚（DESCRIBES → Class，融合查询的接点）
            await self.driver.run(CYPHER_TEMPLATES["merge_doc_node"],
                                  {"doc_id": doc.doc_id,
                                   "source": doc.source})
            report.nodes += 1
            self._done.add(doc.doc_id)
            report.docs_done.append(doc.doc_id)
        return report

    # ---------------------------------------------------------- 分批

    def _batches(self, text: str) -> list[str]:
        """按空行分批，每批 ≤ batch_chars（全库抽取成本高：分批+断点续建）。"""
        paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
        batches: list[str] = []
        cur: list[str] = []
        size = 0
        for para in paras:
            if cur and size + len(para) > self.batch_chars:
                batches.append("\n\n".join(cur))
                cur, size = [], 0
            cur.append(para)
            size += len(para)
        if cur:
            batches.append("\n\n".join(cur))
        return batches or ([text.strip()] if text.strip() else [])

    # ---------------------------------------------------------- 抽取

    async def _extract(self, text: str) -> tuple[list[Entity], list[Triple]]:
        """LLM 抽取 → JSON 解析（fence 容忍 + 坏 JSON 整批丢弃不崩）。"""
        api_sample = ", ".join(sorted(self.catalog._lookup.values())[:200])
        prompt = EXTRACT_PROMPT.format(api=api_sample, text=text)
        try:
            resp = await self.llm.complete(LLMRequest(
                model=self.model, stream=False, temperature=0.0,
                messages=[Message(role="user", content=prompt)]))
            content = resp.content or ""
        except Exception as e:                         # noqa: BLE001
            logger.warning("抽取调用失败，整批丢弃: %s", e)
            return [], []
        m = _JSON_FENCE.search(content)
        raw = m.group(1) if m else content
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("抽取输出不是合法 JSON，整批丢弃: %r", raw[:80])
            return [], []
        nodes = [Entity(name=str(n.get("name", "")).strip(),
                        kind=str(n.get("kind", "")).strip(),
                        uncertain=bool(n.get("uncertain", False)))
                 for n in data.get("nodes", [])
                 if str(n.get("name", "")).strip()
                 and str(n.get("kind", "")).strip() in NODE_KINDS]
        edges = [Triple(src=str(e.get("src", "")).strip(),
                        edge=str(e.get("edge", "")).strip(),
                        dst=str(e.get("dst", "")).strip(),
                        evidence=str(e.get("evidence", "")).strip(),
                        confidence=float(e.get("confidence", 1.0)))
                 for e in data.get("edges", [])
                 if str(e.get("edge", "")).strip() in EDGE_KINDS
                 and str(e.get("src", "")).strip()
                 and str(e.get("dst", "")).strip()]
        return nodes, edges

    # ---------------------------------------------------------- 对齐 + 入图

    def _align(self, entity: Entity, report: BuildReport) -> Entity | None:
        """种子字典归一（§3 的命门）：对不上 → uncertain 队列，不入图。"""
        if entity.kind in ("Concept", "Doc"):
            return entity                 # 概念/文档节点免对齐（自由文本）
        if entity.uncertain:
            report.uncertain.append(entity)
            return None
        canon = self.catalog.canonical(entity.name)
        if canon is None:
            entity.uncertain = True
            report.uncertain.append(entity)
            return None
        entity.name = canon
        return entity

    async def _merge(self, doc: ParsedDoc, nodes: list[Entity],
                     edges: list[Triple], report: BuildReport) -> None:
        # 键 = LLM 原文名（边的 src/dst 用原名引用），值 = 归一后实体
        # ——两个漂移名（area2d / Area 2D）都指向同一个 Area2D 节点
        aligned: dict[str, Entity] = {}
        node_stmts: list[tuple[str, dict]] = []
        edge_stmts: list[tuple[str, dict]] = []
        for ent in nodes:
            original = ent.name
            fixed = self._align(ent, report)
            if fixed is None:
                continue
            aligned[original] = fixed
            if fixed.kind == "Class":
                node_stmts.append((CYPHER_TEMPLATES["merge_class_node"],
                                   {"name": fixed.name, "desc": ""}))
        for tr in edges:
            src = aligned.get(tr.src)
            dst = aligned.get(tr.dst)
            # 端点未对齐/未声明 → 边丢弃（错误的关系比缺失的关系危害大）
            if src is None or dst is None:
                report.dropped.append(f"{tr.src}-[{tr.edge}]->{tr.dst}")
                continue
            # DESCRIBES 的起点钉回当前文档节点（LLM 说了不算，图说了算）
            stmt = self._edge_statement(src, dst, tr, doc)
            if stmt is None:
                report.dropped.append(
                    f"{tr.src}-[{tr.edge}]->{tr.dst}（端点类型不符闭集）")
                continue
            edge_stmts.append(stmt)
        stmts = node_stmts + edge_stmts
        if stmts:
            await self.driver.run_tx(stmts)
        report.nodes += len(aligned)
        report.edges += len(edge_stmts)

    def _edge_statement(self, src: Entity, dst: Entity, tr: Triple,
                        doc: ParsedDoc) -> tuple[str, dict] | None:
        """闭集六类边 → MERGE 模板语句（方向是建模语义，全库统一）。"""
        base = {"evidence": tr.evidence}
        match tr.edge:
            case "INHERITS" if src.kind == "Class" and dst.kind == "Class":
                return (CYPHER_TEMPLATES["merge_inherits_edge"],
                        {"child": src.name, "base": dst.name})
            case "HAS_METHOD" if src.kind == "Class" and dst.kind == "Method":
                return (CYPHER_TEMPLATES["merge_has_method"],
                        {"class": src.name, "name": dst.name, **base})
            case "HAS_SIGNAL" if src.kind == "Class" and dst.kind == "Signal":
                return (CYPHER_TEMPLATES["merge_has_signal"],
                        {"class": src.name, "name": dst.name, **base})
            case "HAS_PROPERTY" if src.kind == "Class" and dst.kind == "Property":
                return (CYPHER_TEMPLATES["merge_has_property"],
                        {"class": src.name, "name": dst.name, **base})
            case "EMITTED_WHEN" if src.kind == "Signal" and dst.kind == "Concept":
                return (CYPHER_TEMPLATES["merge_emitted_when"],
                        {"signal": src.name, "concept": dst.name, **base})
            case "DESCRIBES" if src.kind == "Doc" and dst.kind == "Class":
                return (CYPHER_TEMPLATES["merge_describes_edge"],
                        {"doc_id": doc.doc_id, "class": dst.name})
            case _:
                return None              # 端点类型不符闭集规则 → 丢弃


__all__ = ["ApiCatalog", "ApiGraphBuilder", "BuildReport", "EDGE_KINDS",
           "Entity", "EXTRACT_PROMPT", "NODE_KINDS", "Triple"]
