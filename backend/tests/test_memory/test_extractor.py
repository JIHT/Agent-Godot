"""tests/test_memory/test_extractor.py —— M08 §5：抽取 + 四路决策 + 污染治理。"""
from __future__ import annotations

import json

from agent_godot.agent import Session
from agent_godot.memory import MemoryExtractor, MemoryStore, fake_embed

from .conftest import FakeExtractLLM, make_session_messages, make_store


async def test_extract_adds_new_preferences():
    """假会话抽出约定 + 踩坑，寒暄零抽取。"""
    store = make_store()
    # 抽取结果：2 条 semantic + 1 条 episodic
    extract_result = json.dumps([
        {"kind": "semantic", "content": "信号回调命名 _on_x_y",
         "importance": 0.9, "source": "user_stated"},
        {"kind": "semantic", "content": "GDScript 缩进用 tabs",
         "importance": 0.95, "source": "user_stated"},
        {"kind": "episodic", "content": "Godot 4.3 contacts_reported 改名 max_contacts_reported",
         "importance": 0.6, "source": "model_inferred"},
    ])
    # 三条都是新信息 → 每条决策 ADD（无邻域）
    decide_results = [
        json.dumps({"action": "ADD"}),
        json.dumps({"action": "ADD"}),
        json.dumps({"action": "ADD"}),
    ]
    llm = FakeExtractLLM([extract_result] + decide_results)
    extractor = MemoryExtractor(llm, fake_embed, store, model="cheap",
                                project_id="proj-a")

    session = Session(session_id="s1", messages=make_session_messages())
    report = await extractor.extract_from_session(session, project_id="proj-a")

    assert report.added == 3
    assert report.noop == 0
    active = await store.get_active("proj-a")
    assert len(active) == 3
    store.close()


async def test_contradiction_updates_not_duplicates():
    """矛盾消解：已有"缩进用 spaces" → 新事实"缩进用 tabs" → UPDATE 而非 ADD。"""
    store = make_store()
    # 先存一条旧记忆
    from agent_godot.memory import MemoryRecord
    old_rec = MemoryRecord.make(
        "semantic", "缩进用 spaces", project_id="p", importance=0.7)
    await store.add(old_rec)

    # 抽取出新事实
    extract_result = json.dumps([
        {"kind": "semantic", "content": "缩进用 tabs",
         "importance": 0.9, "source": "user_stated"},
    ])
    # 决策：UPDATE 旧条
    decide_result = json.dumps({
        "action": "UPDATE", "target_id": old_rec.id,
        "merged": "缩进用 tabs（已从 spaces 更新）",
    })
    llm = FakeExtractLLM([extract_result, decide_result])
    extractor = MemoryExtractor(llm, fake_embed, store, project_id="p")

    session = Session(session_id="s1", messages=make_session_messages())
    report = await extractor.extract_from_session(session, project_id="p")

    assert report.updated == 1
    assert report.added == 0
    active = await store.get_active("p")
    assert len(active) == 1        # UPDATE 不是新增——库保持最简自洽
    assert "tabs" in active[0].content
    store.close()


async def test_contradiction_delete_then_add():
    """矛盾 DELETE：旧条归档 + 新条落库。"""
    store = make_store()
    from agent_godot.memory import MemoryRecord
    old_rec = MemoryRecord.make(
        "semantic", "Godot 4.2 用的 contacts_reported", project_id="p")
    await store.add(old_rec)

    extract_result = json.dumps([
        {"kind": "semantic", "content": "Godot 4.3 改用 max_contacts_reported",
         "importance": 0.6, "source": "model_inferred"},
    ])
    decide_result = json.dumps({
        "action": "DELETE", "target_id": old_rec.id,
        "merged": "Godot 4.3 改用 max_contacts_reported",
    })
    llm = FakeExtractLLM([extract_result, decide_result])
    extractor = MemoryExtractor(llm, fake_embed, store, project_id="p")

    session = Session(session_id="s1", messages=make_session_messages())
    report = await extractor.extract_from_session(session, project_id="p")

    assert report.deleted == 1
    # 旧条归档 + 新条 active
    active = await store.get_active("p")
    assert len(active) == 1
    assert "4.3" in active[0].content
    # 旧条还在（软删）
    old = await store.get_by_id(old_rec.id)
    assert old.status == "archived"
    store.close()


async def test_noop_for_duplicates():
    """NOOP：重复信息不入库。"""
    store = make_store()
    from agent_godot.memory import MemoryRecord
    await store.add(MemoryRecord.make(
        "semantic", "信号回调命名 _on_x_y", project_id="p", importance=0.9))

    extract_result = json.dumps([
        {"kind": "semantic", "content": "信号回调命名 _on_x_y",
         "importance": 0.9, "source": "user_stated"},
    ])
    decide_result = json.dumps({"action": "NOOP"})
    llm = FakeExtractLLM([extract_result, decide_result])
    extractor = MemoryExtractor(llm, fake_embed, store, project_id="p")

    session = Session(session_id="s1", messages=make_session_messages())
    report = await extractor.extract_from_session(session, project_id="p")

    assert report.noop == 1
    assert report.added == 0
    active = await store.get_active("p")
    assert len(active) == 1      # 没有新增
    store.close()


async def test_model_inferred_importance_capped():
    """model_inferred 来源的 importance 硬上限 0.6（污染防线②）。"""
    store = make_store()
    extract_result = json.dumps([
        {"kind": "semantic", "content": "项目可能用了组件模式",
         "importance": 0.95, "source": "model_inferred"},  # 应被截到 0.6
    ])
    decide_result = json.dumps({"action": "ADD"})
    llm = FakeExtractLLM([extract_result, decide_result])
    extractor = MemoryExtractor(llm, fake_embed, store, project_id="p")

    session = Session(session_id="s1", messages=make_session_messages())
    await extractor.extract_from_session(session, project_id="p")

    active = await store.get_active("p")
    assert len(active) == 1
    assert active[0].importance <= 0.6       # 被截断
    assert active[0].source == "model_inferred"
    store.close()


async def test_empty_extraction():
    """无值得记的事 → 空报告，不报错。"""
    store = make_store()
    llm = FakeExtractLLM(["[]"])
    extractor = MemoryExtractor(llm, fake_embed, store, project_id="p")

    session = Session(session_id="s1", messages=make_session_messages())
    report = await extractor.extract_from_session(session, project_id="p")
    assert report.total == 0
    store.close()


async def test_extract_with_compressor():
    """有 compressor 时用纪要而非原文做抽取输入。"""
    store = make_store()

    class FakeCompressor:
        async def summarize(self, messages, budget=1500):
            from agent_godot.core import Message
            return Message(role="system",
                           content="目标: 加敌人 | 偏好: tabs 缩进 | 踩坑: 改名")

    extract_result = json.dumps([
        {"kind": "semantic", "content": "tabs 缩进",
         "importance": 0.9, "source": "user_stated"},
    ])
    decide_result = json.dumps({"action": "ADD"})
    llm = FakeExtractLLM([extract_result, decide_result])
    extractor = MemoryExtractor(llm, fake_embed, store,
                                compressor=FakeCompressor(), project_id="p")

    session = Session(session_id="s1", messages=make_session_messages())
    report = await extractor.extract_from_session(session, project_id="p")
    assert report.added == 1
    # 第一次调用应含纪要内容（非 transcript）
    assert "目标" in llm.call_contents[0] or "偏好" in llm.call_contents[0]
    store.close()
