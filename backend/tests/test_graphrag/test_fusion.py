"""图文双引擎融合：推理链渲染 + 双源合证 + 降级（M11 §1.4 / §5 验收）。"""
from __future__ import annotations

from agent_godot.graphrag import (GraphPath, GraphVectorFusion,
                                  ProjectGraphSync)


async def test_graph_path_render():
    """推理链渲染：A --LISTENS--> B --ATTACHED_SCRIPT--> c.gd。"""
    p = GraphPath(nodes=["body_entered", "Main", "main.gd"],
                  edges=["LISTENS", "ATTACHED_SCRIPT"])
    assert p.render() == "body_entered --LISTENS--> Main " \
                         "--ATTACHED_SCRIPT--> main.gd"
    assert GraphPath(nodes=[], edges=[]).render() == ""


async def test_multi_hop_answer_shows_chain(graph, hybrid):
    """验收：多跳问题 → 推理链 + 文档引用双呈现。"""
    fusion = GraphVectorFusion(graph, hybrid)
    ans = await fusion.answer("health_changed 影响哪些脚本", "m06")
    assert ans.graph_paths, "多跳问题必须给出推理链"
    rendered = ans.graph_paths[0].render()
    assert "--LISTENS-->" in rendered
    assert "--ATTACHED_SCRIPT-->" in rendered
    assert "health_changed" in rendered
    # 双源合证：图路径 + 文档引用都在最终答案文本里
    assert "推理链" in ans.answer
    assert "参考文档" in ans.answer
    assert not ans.degraded
    # 文档路确实召回了 chunk（M10 复用）
    assert ans.hits


async def test_degraded_when_graph_empty(driver, sample_project, hybrid):
    """图未建 → 降级纯向量 + 文案建议建图（两种降级文案区分）。"""
    sync = ProjectGraphSync(driver)          # 没跑 full_sync：图未建
    fusion = GraphVectorFusion(sync, hybrid)
    ans = await fusion.answer("body_entered 怎么用", "m06")
    assert ans.degraded
    assert ans.graph_paths == []
    assert "未构建" in ans.note
    assert "参考文档" in ans.answer          # 纯向量兜底仍在


async def test_degraded_note_differs_when_graph_built_but_empty(graph, hybrid):
    """建了图但查不到关系 → 文案是"未发现该关系"（§7 面试 7 ①）。"""
    fusion = GraphVectorFusion(graph, hybrid)
    ans = await fusion.answer("不存在的信号 xyz 影响谁", "m06")
    assert ans.degraded
    assert "未发现" in ans.note
