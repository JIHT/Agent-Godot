"""意图分类：few-shot 主分类 · 规则 fast-path · 缓存 · 非法输出出口（§5）。"""
from __future__ import annotations

from agent_godot.core import Message
from agent_godot.query_engine import Intent, IntentClassifier

from .conftest import FakeLLM


async def test_classify_llm_few_shot(classifier, intent_llm):
    resp = await classifier.classify("Area2D 怎么检测碰撞？", [])
    assert resp is Intent.KNOWLEDGE
    assert intent_llm.calls == 1
    # 提示词带 few-shot 示例与输入
    assert "code_edit" in intent_llm.requests[0].messages[0].content
    assert "Area2D" in intent_llm.requests[0].messages[0].content


async def test_chitchat_fast_path_zero_llm_calls(classifier, intent_llm):
    """规则 fast-path：明显闲聊直判，不花一次调用（§1.1 ②）。"""
    assert await classifier.classify("谢谢！", []) is Intent.CHITCHAT
    assert await classifier.classify("你好", []) is Intent.CHITCHAT
    assert intent_llm.calls == 0


async def test_invalid_label_falls_to_unknown():
    llm = FakeLLM(["我觉得这是一个知识类问题，标签是 knowledge"])
    c = IntentClassifier(llm, model="m")
    assert await c.classify("信号是什么", []) is Intent.UNKNOWN


async def test_cache_hits_on_repeat(classifier, intent_llm):
    """同句重复输入吃缓存：一次调用，两次命中。"""
    q = "信号和回调的区别是什么？"
    assert await classifier.classify(q, []) is Intent.KNOWLEDGE
    assert await classifier.classify(q, []) is Intent.KNOWLEDGE
    assert intent_llm.calls == 1


async def test_llm_failure_is_fail_soft():
    class Boom:
        async def complete(self, req):
            raise RuntimeError("api down")

    c = IntentClassifier(Boom(), model="m")
    assert await c.classify("Area2D 是什么", []) is Intent.UNKNOWN


async def test_history_not_required():
    """签名兼容：history 可传对话历史（few-shot 提示不强制用）。"""
    llm = FakeLLM(["ambiguous"])
    c = IntentClassifier(llm, model="m")
    history = [Message(role="user", content="聊聊 Area2D")]
    assert await c.classify("那第二个呢", history) is Intent.AMBIGUOUS


async def test_empty_input_is_unknown(classifier):
    assert await classifier.classify("", []) is Intent.UNKNOWN
    assert await classifier.classify("   ", []) is Intent.UNKNOWN
