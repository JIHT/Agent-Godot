"""查询改写：fast-path 跳过 · 指代消解 · HyDE（M12 §1.2 / §5）。"""
from __future__ import annotations

from agent_godot.core import Message
from agent_godot.query_engine import QueryRewriter

from .conftest import FakeLLM


def test_needs_rewrite_detects_pronouns(rewriter):
    """代词/省略句需要改写；完整独立句跳过（省一次调用）。"""
    assert rewriter._needs_rewrite("那它的信号呢")
    assert rewriter._needs_rewrite("继续")
    assert rewriter._needs_rewrite("呢？")
    assert not rewriter._needs_rewrite(
        "Area2D 的 body_entered 信号在什么条件下触发")
    assert not rewriter._needs_rewrite("")


async def test_complete_sentence_skips_llm(rewriter, rewriter_llm):
    q = "CharacterBody2D 的 move_and_slide 返回值是什么意思"
    assert await rewriter.rewrite(q, []) == q
    assert rewriter_llm.calls == 0


async def test_followup_rewritten_with_history(rewriter, rewriter_llm):
    """验收 §5：上文聊 Area2D，追问"那它的信号" → 改写结果含上文实体。"""
    history = [
        Message(role="user", content="Area2D 怎么检测碰撞？"),
        Message(role="assistant", content="用 body_entered 信号。"),
    ]
    out = await rewriter.rewrite("那它的信号呢", history)
    assert out == "Area2D 的检测信号 body_entered"
    # 提示词含历史摘要 + 原句，且"只改写不回答"纪律在提示里
    prompt = rewriter_llm.requests[0].messages[0].content
    assert "Area2D" in prompt and "那它的信号呢" in prompt
    assert "只改写不回答" in prompt


async def test_no_history_no_rewrite(rewriter, rewriter_llm):
    """无上文可消解：改写无从下手，原样返回。"""
    assert await rewriter.rewrite("那它呢", []) == "那它呢"
    assert rewriter_llm.calls == 0


async def test_rewrite_failure_returns_original():
    class Boom:
        async def complete(self, req):
            raise RuntimeError("api down")

    r = QueryRewriter(Boom(), model="m")
    assert await r.rewrite("那它的信号呢", [
        Message(role="user", content="聊聊 Area2D")]) == "那它的信号呢"


async def test_rewrite_takes_first_line_only(rewriter):
    """模型顺手多解释 → 只取第一行（改写要的是一行查询）。"""
    llm = FakeLLM(["Area2D 的检测信号\n补充说明：这里还可以……"])
    r = QueryRewriter(llm, model="m")
    out = await r.rewrite("那它的信号呢", [
        Message(role="user", content="聊聊 Area2D")])
    assert out == "Area2D 的检测信号"


async def test_hyde_generates_pseudo_document():
    llm = FakeLLM(["body_entered 信号在 monitoring=true 且 monitorable="
                   "true 时触发，处理函数挂在碰撞双方节点上。"])
    r = QueryRewriter(llm, model="m")
    doc = await r.hyde("碰撞检测信号")
    assert doc.startswith("body_entered")


async def test_hyde_failure_returns_query():
    class Boom:
        async def complete(self, req):
            raise RuntimeError("api down")

    r = QueryRewriter(Boom(), model="m")
    assert await r.hyde("碰撞检测信号") == "碰撞检测信号"
