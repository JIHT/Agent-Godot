"""API 图谱构建：LLM 抽取 + 种子对齐 + 幂等 + 断点续建（M11 §1.2/§3/§4 步骤 4）。"""
from __future__ import annotations

import json

from agent_godot.core.llm import LLMResponse
from agent_godot.graphrag import (ApiCatalog, ApiGraphBuilder, BuildReport)
from agent_godot.rag import ParsedDoc

SEED = ["Area2D", "CollisionObject2D", "CharacterBody2D", "Node2D",
        "body_entered", "monitoring", "move_and_slide"]

# 假模型剧本：名称漂移（大小写/空格）+ uncertain 实体 + 端点缺失的边
FAKE_JSON = json.dumps({
    "nodes": [
        {"name": "area2d", "kind": "Class"},
        {"name": "Area 2D", "kind": "Class"},           # 同类漂移 → 归一合并
        {"name": "COLLISIONOBJECT2d", "kind": "Class"},
        {"name": "body_entered", "kind": "Signal"},
        {"name": "监测体进入", "kind": "Concept"},       # Concept 免对齐
        {"name": "WibbleBlorp", "kind": "Class"},        # 种子外 → uncertain
    ],
    "edges": [
        {"src": "area2d", "edge": "INHERITS",
         "dst": "COLLISIONOBJECT2d", "evidence": "Area2D 继承 CollisionObject2D"},
        {"src": "area2d", "edge": "HAS_SIGNAL", "dst": "body_entered",
         "evidence": "body_entered 信号"},
        {"src": "body_entered", "edge": "EMITTED_WHEN", "dst": "监测体进入",
         "evidence": "monitoring 开启时触发"},
        {"src": "NopeClass", "edge": "INHERITS", "dst": "area2d",
         "evidence": "端点未声明 → 丢"},
        {"src": "area2d", "edge": "HAS_METHOD", "dst": "frobnicate",
         "evidence": "种子外端点 → 丢"},
    ],
}, ensure_ascii=False)


class FakeExtractLLM:
    """抽取用假模型：complete 返回预置 JSON（test_memory 同款思路）。"""

    def __init__(self, content: str = FAKE_JSON):
        self.content = content
        self.calls = 0

    async def complete(self, req) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.content, tool_calls=[],
                           usage=None, finish_reason="stop")


def _doc(text: str) -> ParsedDoc:
    return ParsedDoc.make(source="docs/area2d.md", kind="md", text=text)


def test_catalog_canonical():
    """种子字典归一：排版差异合并；语义差异（下划线数量）绝不合并。"""
    cat = ApiCatalog(SEED)
    assert cat.canonical("area2d") == "Area2D"
    assert cat.canonical("Area 2D") == "Area2D"
    assert cat.canonical("CHARACTERBODY2D") == "CharacterBody2D"
    assert cat.canonical("WibbleBlorp") is None
    # 宁漏勿错：get_node / get_nodes 是两个 API（种子外一律 None 兜底）
    cat2 = ApiCatalog(["get_node", "get_nodes"])
    assert cat2.canonical("get_node") == "get_node"
    assert cat2.canonical("get_nodes") == "get_nodes"


async def test_build_from_docs_aligns_and_reports(driver):
    """抽取 → 对齐 → 入图：漂移归一、种子外进 uncertain、端点缺失边丢弃。"""
    llm = FakeExtractLLM()
    builder = ApiGraphBuilder(driver, llm, ApiCatalog(SEED))
    report = await builder.build_from_docs([_doc("Area2D 文档正文")])

    assert isinstance(report, BuildReport)
    # 漂移合并：area2d / Area 2D → 一个 Area2D 节点
    assert driver.count("Class", name="Area2D") == 1
    assert driver.count("Class", name="CollisionObject2D") == 1
    # uncertain 队列：WibbleBlorp 待人工审，不入图
    assert [e.name for e in report.uncertain] == ["WibbleBlorp"]
    assert driver.count("Class", name="WibbleBlorp") == 0
    # 端点缺失/种子外的边丢弃（错误的关系比缺失的关系危害大）
    assert len(report.dropped) == 2
    # 3 条合法边 + Doc 节点（图文互链）
    assert report.edges == 3
    assert driver.count("Doc") == 1


async def test_build_is_idempotent(driver):
    """跑两遍图不变（MERGE + 唯一键 = 幂等；§7 面试 4）。"""
    llm = FakeExtractLLM()
    builder = ApiGraphBuilder(driver, llm, ApiCatalog(SEED))
    doc = _doc("Area2D 文档正文")
    await builder.build_from_docs([doc])
    n_before = len(driver._nodes)
    report2 = await builder.build_from_docs([doc])
    assert len(driver._nodes) == n_before
    # 断点续建：第二遍 doc 已在进度表 → 零抽取调用
    assert report2.docs_done == []
    assert llm.calls == 1


async def test_bad_json_batch_is_dropped_not_crashed(driver):
    """坏 JSON 整批丢弃不崩（全库抽取长跑的容错底线）。"""
    llm = FakeExtractLLM(content="这不是 JSON")
    builder = ApiGraphBuilder(driver, llm, ApiCatalog(SEED))
    report = await builder.build_from_docs([_doc("正文")])
    assert report.edges == 0
    assert report.nodes == 1              # 只剩 Doc 节点
