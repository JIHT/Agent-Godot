"""tests/test_hooks/test_pipeline.py —— Hook 管线的四条宪法（M14 §1.1 / §5）

断言全部用"计数器 log"：谁跑了、看到什么、谁没跑——这是插件协议的可回归性基础。
"""
from __future__ import annotations

import asyncio

import pytest

from agent_godot.hooks import (HookContext, HookPipeline, HookResult,
                               HookSpec, HookVeto)

from .conftest import counting_hook


def _ctx(**kw) -> HookContext:
    return HookContext(point="pre_tool", tool="echo", args={"x": "0"}, **kw)


async def test_hooks_run_in_priority_order():
    """priority 小者先跑，且与注册顺序无关（稳定排序）。"""
    log: list = []
    pipeline = HookPipeline()
    pipeline.register(counting_hook("late", priority=20, log=log))
    pipeline.register(counting_hook("early", priority=5, log=log))

    await pipeline.run("pre_tool", _ctx())
    assert [e["name"] for e in log] == ["early", "late"]


async def test_same_priority_sorted_by_name_for_stability():
    """同段位按名字排——注册顺序不该影响结果（否则测试无法钉死）。"""
    log: list = []
    pipeline = HookPipeline()
    pipeline.register(counting_hook("zebra", priority=10, log=log))
    pipeline.register(counting_hook("alpha", priority=10, log=log))
    await pipeline.run("pre_tool", _ctx())
    assert [e["name"] for e in log] == ["alpha", "zebra"]


async def test_veto_short_circuits_pipeline():
    """§5：priority 5 的 veto 阻止 priority 10 的 hook 执行。"""
    log: list = []
    pipeline = HookPipeline()
    pipeline.register(counting_hook("blocker", priority=5, log=log,
                                    result=HookResult.veto("不安全")))
    pipeline.register(counting_hook("never", priority=10, log=log))

    with pytest.raises(HookVeto) as exc:
        await pipeline.run("pre_tool", _ctx())

    assert [e["name"] for e in log] == ["blocker"]      # 短路：后面的没跑
    assert exc.value.hook_name == "blocker"
    assert exc.value.reason == "不安全"
    assert pipeline.veto_events()[0]["hook"] == "blocker"


async def test_modify_chain_order():
    """§5：A(p=10) 先加前缀、B(p=20) 后加后缀，且 B 看到 A 改过的参数。"""
    log: list = []

    def plus_prefix(ctx: HookContext):
        log.append(("A", dict(ctx.args), list(ctx.modified_by)))
        return HookResult.modify(args={"x": f"a-{ctx.args['x']}"}, reason="A 加前缀")

    def plus_suffix(ctx: HookContext):
        log.append(("B", dict(ctx.args), list(ctx.modified_by)))
        return HookResult.modify(args={"x": f"{ctx.args['x']}-b"}, reason="B 加后缀")

    pipeline = HookPipeline()
    pipeline.register(HookSpec(name="B", point="pre_tool", priority=20,
                               handler=plus_suffix))
    pipeline.register(HookSpec(name="A", point="pre_tool", priority=10,
                               handler=plus_prefix))

    out = await pipeline.run("pre_tool", _ctx(args={"x": "0"}))
    assert out.args == {"x": "a-0-b"}                   # 链式：不是"后见覆盖先见"
    assert log[0][1] == {"x": "0"}                      # A 看到原始参数
    assert log[1][1] == {"x": "a-0"}                    # ★ B 看到 A 改过的
    assert out.modified_by == ["A", "B"]                # 审计：谁改过


async def test_async_hooks_joined_at_session_end():
    """§5：异步 hook 不阻塞管线，session_end 后全部完成。"""
    done: list[str] = []

    async def slow(ctx: HookContext):
        await asyncio.sleep(0.05)
        done.append("slow")
        return None

    pipeline = HookPipeline()
    pipeline.register(HookSpec(name="reporter", point="post_tool", priority=100,
                               handler=slow, async_=True))

    await pipeline.run("post_tool", _ctx())
    assert done == []                                    # 没等——后台跑
    assert pipeline.pending_background() == 1

    waited = await pipeline.join_background()
    assert waited == 1
    assert done == ["slow"]
    assert pipeline.pending_background() == 0


async def test_sync_hooks_still_block():
    """对照组：同步 hook 必须等（它在决策链上，能 veto/modify）。"""
    done: list[str] = []

    async def sync_hook(ctx: HookContext):
        await asyncio.sleep(0.01)
        done.append("sync")
        return None

    pipeline = HookPipeline()
    pipeline.register(HookSpec(name="sync", point="post_tool", priority=100,
                               handler=sync_hook))
    await pipeline.run("post_tool", _ctx())
    assert done == ["sync"]


async def test_handler_exception_is_swallowed_and_traced():
    """横切逻辑不许炸主流程：hook 抛异常 → 记 trace + 当 pass。"""
    async def boom(ctx: HookContext):
        raise RuntimeError("boom")

    log: list = []
    pipeline = HookPipeline()
    pipeline.register(HookSpec(name="boom", point="pre_tool", priority=1,
                               handler=boom))
    pipeline.register(counting_hook("after", priority=2, log=log))

    out = await pipeline.run("pre_tool", _ctx())
    assert [e["name"] for e in log] == ["after"]         # 后面的照常跑
    assert pipeline.trace[0]["action"] == "error"
    assert out.args == {"x": "0"}


async def test_slow_hook_timeout_skips_it():
    """超时保护：单 hook 卡住不能拖死整条管线（§7 开放题④的运行时限速）。"""
    async def stuck(ctx: HookContext):
        await asyncio.sleep(1.0)
        return HookResult.modify(args={"x": "stuck"})

    pipeline = HookPipeline(hook_timeout=0.02)
    pipeline.register(HookSpec(name="stuck", point="pre_tool", priority=1,
                               handler=stuck))
    out = await pipeline.run("pre_tool", _ctx())
    assert out.args == {"x": "0"}                        # 被跳过，未生效
    assert pipeline.trace[0]["action"] == "timeout"


async def test_unregister_and_unknown_point():
    pipeline = HookPipeline()
    spec = counting_hook("perm", priority=0)
    pipeline.register(spec)
    assert pipeline.has("pre_tool")
    assert pipeline.unregister("perm") is True
    assert pipeline.has("pre_tool") is False
    assert pipeline.unregister("perm") is False

    with pytest.raises(ValueError):
        await pipeline.run("not_a_point", _ctx())
    with pytest.raises(ValueError):                      # 注册时就拦住非法挂载点
        pipeline.register(HookSpec(name="x", point="nope", handler=lambda c: None))


async def test_pre_loop_messages_injected():
    """pre_loop 的注入位：hook 返回的消息被累加进 ctx.messages。"""
    from agent_godot.core import Message

    async def injector(ctx: HookContext):
        return HookResult.modify(messages=[Message(role="system", content="预算告警")])

    pipeline = HookPipeline()
    pipeline.register(HookSpec(name="budget", point="pre_loop", priority=10,
                               handler=injector))
    out = await pipeline.run("pre_loop", HookContext(point="pre_loop"))
    assert [m.content for m in out.messages] == ["预算告警"]
