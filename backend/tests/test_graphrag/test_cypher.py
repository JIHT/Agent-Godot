"""模板库与内存驱动的表驱动测试（M11 §4 步骤 2 验证）。"""
from __future__ import annotations

import pytest

from agent_godot.graphrag import (CYPHER_TEMPLATES, InMemoryGraphDriver,
                                  QUERY_TEMPLATES, query)

# 表驱动：模板名 → 空库可用的最小参数（LIMIT 注入的验收面）
TEMPLATE_PARAMS = {
    "project_exists": {"project": "p"},
    "signal_listeners": {"signal": "s", "project": "p", "limit": 10},
    "impact_of_signal": {"signal": "s", "project": "p", "limit": 10},
    "dead_signals": {"project": "p", "limit": 10},
    "inherits_chain": {"name": "X", "limit": 10},
    "shortest_script_dep": {"project": "p", "from": "a", "to": "b"},
    "signals_of_class": {"name": "X", "limit": 10},
    "emitted_when": {"name": "s", "limit": 10},
    "all_signals_of_project": {"project": "p", "limit": 10},
}


@pytest.mark.parametrize("name", TEMPLATE_PARAMS)
async def test_query_template_runs_on_empty_graph(name):
    """九个查询模板空库全不炸（表驱动参数注入 + 返回 list[dict]）。"""
    rows = await query(InMemoryGraphDriver(), name, **TEMPLATE_PARAMS[name])
    assert isinstance(rows, list)
    assert all(isinstance(r, dict) for r in rows)


def test_templates_are_parameterized():
    """防注入铁律：查询模板一律带 $limit 或 count（禁 f-string 拼 Cypher）。"""
    limited = {"project_exists", "shortest_script_dep"}
    for name in QUERY_TEMPLATES:
        text = CYPHER_TEMPLATES[name]
        assert "$" in text, f"{name} 未参数化"
        if name not in limited:
            assert "LIMIT" in text, f"{name} 缺 LIMIT 注入"


def test_bounded_depth_only():
    """变长路径一律限深（裸 * 是性能炸弹）。"""
    import re
    unbounded = re.compile(r"\*(?![\d.])")   # * 后面不是 数字/..（如 [:INHERITS*]）
    for name, text in CYPHER_TEMPLATES.items():
        m = unbounded.search(text)
        assert m is None, f"{name} 含无界变长路径（应 *1..N 限深）"



async def test_memory_driver_rejects_unknown_cypher():
    """内存驱动只认模板库闭集（任意 Cypher 是 Neo4j 的活）。"""
    driver = InMemoryGraphDriver()
    with pytest.raises(ValueError, match="模板库"):
        await driver.run("MATCH (n) RETURN n", {})


async def test_merge_is_idempotent():
    """MERGE 幂等：同一语句跑两遍节点不翻倍（唯一键约束的等价物）。"""
    driver = InMemoryGraphDriver()
    stmt = CYPHER_TEMPLATES["merge_class_node"]
    await driver.run(stmt, {"name": "Area2D", "desc": ""})
    await driver.run(stmt, {"name": "Area2D", "desc": "x"})
    assert driver.count("Class") == 1
