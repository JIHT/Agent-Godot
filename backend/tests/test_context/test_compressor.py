"""tests/test_context/test_compressor.py —— M07 §5：B 档模板 / C 档 LLM / 降级兜底。"""
from __future__ import annotations

from agent_godot.context import Compressor
from agent_godot.core import Message

from .conftest import ExplodingLLM, FakeSummaryLLM, make_session_messages


def test_template_digest_two_line_style():
    """B 档：每轮抽 user 首行 + assistant 结论尾行，零 LLM 成本。"""
    msgs = make_session_messages(turns=3)
    # 加一轮带 assistant 纯文本结论
    msgs.append(Message(role="assistant", content="第一步：分析完成。\n敌人已创建。"))
    c = Compressor()
    digest = c.template_digest(msgs)
    assert digest.role == "system"
    assert "完整原文已省略" in digest.content
    assert "用户: 帮我加一个敌人" in digest.content
    assert "助手: 敌人已创建。" in digest.content
    assert "调用工具 echo" in digest.content


async def test_llm_summarize_uses_cheap_model():
    """C 档：调 complete 生成结构化纪要（五段格式 + 宣告原文省略）。"""
    llm = FakeSummaryLLM()
    c = Compressor(llm=llm, model="cheap-model")
    msgs = make_session_messages(turns=5)
    summary = await c.summarize(msgs, budget=800)
    assert summary.role == "system"
    assert "mode=llm" in summary.content
    assert "完整原文已省略" in summary.content
    assert "enemy.gd(a3f2)" in summary.content       # 文件清单带 hash
    assert llm.calls == 1


async def test_llm_summarize_falls_back_to_template_on_failure():
    """C 档失败（后端炸）→ B 档兜底，压缩永不拖死主流程。"""
    c = Compressor(llm=ExplodingLLM(), model="cheap")
    msgs = make_session_messages(turns=4)
    summary = await c.summarize(msgs)
    assert "mode=template" in summary.content


async def test_summarize_without_llm_uses_template():
    """无 LLM（离线/未配置）→ C 档自动降级 B 档。"""
    c = Compressor()
    msgs = make_session_messages(turns=4)
    summary = await c.summarize(msgs)
    assert "mode=template" in summary.content


async def test_summarize_empty_segment():
    c = Compressor()
    summary = await c.summarize([])
    assert summary.role == "system"
