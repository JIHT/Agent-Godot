"""tests/test_context/test_token_counter.py —— M07 §5：估算 sanity + 校准收敛。"""
from __future__ import annotations

from agent_godot.context import TokenCounter
from agent_godot.core import Message, ToolCall


def test_estimate_mixed_content():
    """中文 0.6/字、非中文 4 字符/token、每消息 +4 结构开销。"""
    tc = TokenCounter()
    m = Message(role="user", content="玩家击败了敌人")   # 8 个汉字
    est = tc.estimate([m])
    assert 8 <= est <= 12                              # ≈ 4(结构)+4(汉字)+2(尾)


def test_estimate_counts_tool_call_arguments():
    args = '{"path": "res://enemy.gd", "content": "' + "x" * 400 + '"}'
    plain = Message(role="user", content="hi")
    with_calls = Message(role="assistant",
                         tool_calls=[ToolCall(id="c1", name="write",
                                              arguments=args)])
    assert tc_delta(with_calls, plain) > 100


def tc_delta(a, b) -> int:
    return TokenCounter().estimate([a]) - TokenCounter().estimate([b])


def test_estimate_tools_counts_schema():
    from agent_godot.core import ToolSpec
    spec = ToolSpec(name="write_file", description="写文件 " * 50,
                    parameters={"type": "object",
                                "properties": {"path": {"type": "string"}}})
    tc = TokenCounter()
    assert tc.estimate_tools([spec]) > 100
    assert tc.estimate_tools(None) == 0


def test_exact_falls_back_to_estimate_without_tiktoken():
    """tiktoken 缺席（未安装）→ 精算退回估算，不炸。"""
    tc = TokenCounter()
    msgs = [Message(role="user", content="hello 世界")]
    assert tc.exact(msgs, "test-model") == tc.estimate(msgs)


def test_calibration_converges():
    """注入固定偏差的 usage 回执 20 次 → 系数被校准，误差下降。"""
    tc = TokenCounter(cjk_ratio=0.6)
    msgs = [Message(role="user", content="字" * 1000)]    # 纯中文

    def reported() -> int:
        # 假设真实 tokenizer：1.0 token/字 + 6 结构开销（系统性低估 40%）
        return 1000 + 6

    def estimated() -> int:
        return tc.estimate(msgs)

    err0 = abs(reported() - estimated()) / reported()
    assert err0 > 0.30                                  # 初始确实低估

    for _ in range(20):
        tc.calibrate(reported(), estimated())

    err1 = abs(reported() - estimated()) / reported()
    assert err1 < 0.10                                  # 校准后误差收敛
    assert err1 < err0
    assert tc.cjk_ratio > 0.6                           # 低估 → 系数调大


def test_calibration_no_op_within_tolerance():
    """误差 ≤10% 不动系数（防阈值附近抖动）。"""
    tc = TokenCounter(cjk_ratio=0.6)
    before = tc.cjk_ratio
    tc.calibrate(100, 95)                               # 误差 5%
    assert tc.cjk_ratio == before
