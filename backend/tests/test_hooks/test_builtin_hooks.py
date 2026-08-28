"""tests/test_hooks/test_builtin_hooks.py —— 三个内置 hook + Dispatcher 翻译层（M14 §3）

Dispatcher 那几条是"插件协议宪法"：veto 必须变协议化 Observation（不能直接
上抛炸 Loop）、确认门批准的调用只能执行一次且只记一次账、pre_tool 改的参数
必须真的流进工具。
"""
from __future__ import annotations

import pytest

from agent_godot.agent import Dispatcher
from agent_godot.core import ToolCall
from agent_godot.hooks import (FormatHook, HookContext, HookPipeline, HookResult,
                               HookSpec, HookVeto, PermissionHook, RedactHook,
                               build_default_pipeline, format_gdscript)
from agent_godot.permission.gate import GateDecision
from agent_godot.tools import ErrorKind, ToolResponse
from agent_godot.tools.file_lock import sha16

from .conftest import EchoTool, make_dispatcher


class FakeGate:
    """判定门替身：check 返回预置决策；request 走 dispatcher 现场执行。"""

    def __init__(self, action: str = "allow", reason: str = "",
                 dispatcher=None):
        self.action = action
        self.reason = reason
        self.dispatcher = dispatcher
        self.checked: list[ToolCall] = []
        self.requested: list[ToolCall] = []

    async def check(self, call: ToolCall) -> GateDecision:
        self.checked.append(call)
        return GateDecision(self.action, self.reason)

    async def request(self, call: ToolCall) -> ToolResponse:
        self.requested.append(call)
        if self.dispatcher is None:
            return ToolResponse(ok=True, summary="fake-approved")
        return await self.dispatcher.execute_now(call)


def _pipeline(*specs) -> HookPipeline:
    p = HookPipeline()
    for s in specs:
        p.register(s)
    return p


# ---------- PermissionHook ----------

async def test_permission_hook_allow_passes():
    gate = FakeGate("allow")
    hook = PermissionHook(gate, dispatcher=None)
    out = await hook(HookContext(point="pre_tool", call_id="c1", tool="echo",
                                 args={"x": "hi"}))
    assert out is None                                  # pass
    assert [c.name for c in gate.checked] == ["echo"]
    assert gate.checked[0].arguments == '{"x": "hi"}'   # 参数被还原成 ToolCall


async def test_permission_hook_deny_vetoes_with_reason():
    gate = FakeGate("deny", reason="规则禁止")
    out = await PermissionHook(gate)(HookContext(point="pre_tool", tool="rm",
                                                 args={}))
    assert out is not None and out.action == "veto"
    assert out.reason == "规则禁止"
    assert out.modified_response is None                # 无响应 → Dispatcher 拼 DENIED


async def test_permission_hook_confirm_carries_response():
    """need_confirm：确认门自己执行完，把响应带回去（否则会被执行两次）。"""
    dispatcher = make_dispatcher()
    gate = FakeGate("need_confirm", reason="需用户确认", dispatcher=dispatcher)
    out = await PermissionHook(gate, dispatcher)(
        HookContext(point="pre_tool", call_id="c1", tool="echo",
                    args={"x": "hi"}))
    assert out is not None and out.action == "veto"      # 短路：别再执行第二次
    assert out.modified_response is not None
    assert out.modified_response.ok is True
    assert out.modified_response.summary == "hi"
    assert out.reported is True                          # 确认门内部已记账


async def test_permission_hook_without_confirm_gate_vetoes():
"""只有判定门（无 request）时：不擅自执行，按"需确认"短路。"""

class JudgmentOnlyGate(FakeGate):
    request = None                                   # 模拟 PermissionGate（无确认门）

gate = JudgmentOnlyGate("need_confirm")
out = await PermissionHook(gate)(
    HookContext(point="pre_tool", tool="echo", args={}))
assert out is not None and out.action == "veto"
assert "未挂载确认门" in out.reason


# ---------- Dispatcher × HookVeto 翻译层（§3） ----------

async def test_dispatcher_veto_becomes_denied_response():
    """veto 不能上抛炸 Loop——必须变成 ok=False 的 Observation。"""
    dispatcher = make_dispatcher()
    gate = FakeGate("deny", reason="危险操作")
    reported: list = []
    dispatcher.on_result = lambda call, resp: reported.append(call.id)
    dispatcher.hooks = _pipeline(PermissionHook(gate, dispatcher).spec())

    results = await dispatcher.execute(
        [ToolCall(id="c1", name="echo", arguments='{"x": "hi"}')])
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error is not None
    assert results[0].error.kind is ErrorKind.DENIED
    assert "被 hook 拦截: permission" in results[0].error.message
    assert results[0].error.hint == "危险操作"           # 理由 → hint（模型照做换方案）
    assert reported == ["c1"]                            # 拒绝也是一次完成，记账


async def test_dispatcher_confirm_runs_once_and_reports_once():
    """§3 铁律：确认门批准后执行且仅执行一次，事件流里只有一条 ToolDone。"""
    runs: list[str] = []

    class CountingEcho(EchoTool):
        async def run(self, x: str = "ok") -> ToolResponse:
            runs.append(x)
            return ToolResponse(ok=True, summary=x)

    dispatcher = make_dispatcher()
    dispatcher.registry.register(CountingEcho())
    gate = FakeGate("need_confirm", dispatcher=dispatcher)
    reported: list = []
    dispatcher.on_result = lambda call, resp: reported.append(call.id)
    dispatcher.hooks = _pipeline(PermissionHook(gate, dispatcher).spec())

    results = await dispatcher.execute(
        [ToolCall(id="c1", name="echo", arguments='{"x": "hi"}')])
    assert results[0].ok is True and results[0].summary == "hi"
    assert runs == ["hi"]                                # 恰好一次
    assert reported == ["c1"]                            # 恰好一次（reported 标记生效）
    assert len(gate.requested) == 1


async def test_dispatcher_pre_tool_modify_reaches_tool():
    """pre_tool 改的参数必须真的流进工具（不能只是改了个 ctx 摆着）。"""
    async def rewrite(ctx: HookContext):
        return HookResult.modify(args={"x": "rewritten"}, reason="测试改写")

    dispatcher = make_dispatcher()
    dispatcher.hooks = _pipeline(
        HookSpec(name="rewrite", point="pre_tool", priority=50, handler=rewrite))
    results = await dispatcher.execute(
        [ToolCall(id="c1", name="echo", arguments='{"x": "original"}')])
    assert results[0].summary == "rewritten"


async def test_dispatcher_post_tool_modifies_observation():
    """post_tool 改写的响应是模型看到的 Observation（`or response` 语义）。"""
    async def decorate(ctx: HookContext):
        resp = ctx.response
        return HookResult.modify(
            response=ToolResponse(ok=resp.ok, summary=resp.summary + " [已装饰]"),
            reason="测试改写")

    dispatcher = make_dispatcher()
    dispatcher.hooks = _pipeline(
        HookSpec(name="decorate", point="post_tool", priority=100,
                 handler=decorate))
    results = await dispatcher.execute(
        [ToolCall(id="c1", name="echo", arguments='{"x": "hi"}')])
    assert results[0].summary == "hi [已装饰]"
    assert results[0].call_id == "c1"                    # call_id 不能被改写弄丢


async def test_dispatcher_without_pre_tool_hooks_keeps_inline_gate():
    """没挂 pre_tool hook 时沿用 M09 内联门禁（M09 测试零回归的保证）。"""
    dispatcher = make_dispatcher()
    gate = FakeGate("deny", reason="内联拒")
    dispatcher.gate = gate
    results = await dispatcher.execute(
        [ToolCall(id="c1", name="echo", arguments='{"x": "hi"}')])
    assert results[0].ok is False
    assert results[0].error.kind is ErrorKind.DENIED
    assert len(gate.checked) == 1


# ---------- FormatHook ----------

UGLY_GD = 'extends Node\nclass_name Player\n\n\nfunc _ready():\n    print("hi")   \n'
CLEAN_GD = 'extends Node\nclass_name Player\n\nfunc _ready():\n\tprint("hi")\n'


def test_format_gdscript_rules():
    """三规则：行尾空白 / 4 空格→Tab / 空行归一 + 文末恰好一个换行。"""
    assert format_gdscript(UGLY_GD) == CLEAN_GD
    assert format_gdscript(CLEAN_GD) == CLEAN_GD          # 幂等（第二次不动）
    assert format_gdscript("") == ""
    assert format_gdscript("\n\n\n") == ""


def test_format_gdscript_keeps_triple_quoted_blocks():
    """多行字符串内部原样保留——格式化不能改变语义。"""
    text = 'var s = """\n  raw   text\n\n\n  end\n"""\n'
    assert format_gdscript(text) == 'var s = """\n  raw   text\n\n\n  end\n"""\n'


async def test_format_hook_formats_file_and_refreshes_hash(tmp_path):
    """格式化 + 乐观锁联动：不更新 hash，模型下一次 write 必然 CONFLICT。"""
    (tmp_path / "player.gd").write_text(UGLY_GD, encoding="utf-8")
    resp = ToolResponse(ok=True, summary="已写入 player.gd", data={"hash": "old"})
    ctx = HookContext(point="post_tool", tool="write_script",
                      args={"path": "player.gd"}, response=resp)

    out = await FormatHook(tmp_path)(ctx)
    assert out is not None and out.action == "modify"
    assert (tmp_path / "player.gd").read_text(encoding="utf-8") == CLEAN_GD
    assert out.response.data["hash"] == sha16(CLEAN_GD)   # ★ 新 hash
    assert "format hook" in out.response.summary


async def test_format_hook_skips_clean_or_irrelevant(tmp_path):
    """已干净 / 非 .gd / 失败响应 / 文件不存在 → 一律 pass（不做无用 IO）。"""
    (tmp_path / "a.gd").write_text(CLEAN_GD, encoding="utf-8")
    hook = FormatHook(tmp_path)
    base = ToolResponse(ok=True, summary="x")
    assert await hook(HookContext(point="post_tool", tool="write_script",
                                  args={"path": "a.gd"}, response=base)) is None
    assert await hook(HookContext(point="post_tool", tool="write_script",
                                  args={"path": "a.txt"}, response=base)) is None
    assert await hook(HookContext(point="post_tool", tool="write_script",
                                  args={"path": "missing.gd"},
                                  response=base)) is None
    assert await hook(HookContext(point="post_tool", tool="write_script",
                                  args={"path": "a.gd"},
                                  response=ToolResponse(
                                      ok=False, summary="写入失败"))) is None


async def test_format_hook_respects_tool_whitelist(tmp_path):
    (tmp_path / "a.gd").write_text(UGLY_GD, encoding="utf-8")
    hook = FormatHook(tmp_path, tools={"godot_write_script"})
    ctx = HookContext(point="post_tool", tool="write_script",
                      args={"path": "a.gd"},
                      response=ToolResponse(ok=True, summary="x"))
    assert await hook(ctx) is None                       # 不在白名单 → 不动
    ctx2 = HookContext(point="post_tool", tool="godot_write_script",
                       args={"path": "a.gd"},
                       response=ToolResponse(ok=True, summary="x"))
    assert await hook(ctx2) is not None                  # 白名单内 → 格式化


# ---------- RedactHook ----------

async def test_redact_hook_masks_secrets_in_summary_and_data():
    resp = ToolResponse(ok=True,
                        summary="api_key=sk-abcdefghijklmnop password: hunter2222",
                        data={"token": "abc123xyz", "nested": {"secret": "s3cr3t"}})
    out = await RedactHook()(HookContext(point="post_tool", tool="echo",
                                         response=resp))
    assert out is not None and out.action == "modify"
    assert "sk-abcdefghijklmnop" not in out.response.summary
    assert "hunter2222" not in out.response.summary
    assert out.response.data["token"] == "***"
    assert out.response.data["nested"]["secret"] == "***"


async def test_redact_hook_passes_when_nothing_matches():
    resp = ToolResponse(ok=True, summary="一切正常，没有凭据")
    assert await RedactHook()(HookContext(point="post_tool", tool="echo",
                                          response=resp)) is None


async def test_redact_hook_ignores_gdscript_typed_declaration():
    """`var token := "x"` 是声明不是泄漏——误伤会把代码搅乱。"""
    resp = ToolResponse(ok=True, summary='var token := "x"\nvar hp := 10')
    assert await RedactHook()(HookContext(point="post_tool", tool="echo",
                                          response=resp)) is None


# ---------- build_default_pipeline ----------

def test_build_default_pipeline_order(tmp_path):
    """段位即顺序：permission(0) → redact(90) → format(100)。"""
    pipeline = build_default_pipeline(gate=FakeGate(), dispatcher=None,
                                      format_root=tmp_path)
    assert pipeline.names("pre_tool") == ["permission"]
    assert pipeline.names("post_tool") == ["redact", "format"]

    pipeline.unregister("permission")                    # 测试环境一行关门禁
    assert not pipeline.has("pre_tool")


async def test_hook_veto_carries_reported_flag():
    with pytest.raises(HookVeto) as exc:
        await _pipeline(HookSpec(
            name="x", point="pre_tool", priority=1,
            handler=lambda ctx: HookResult.veto(
                "r", response=ToolResponse(ok=True, summary="done"),
                reported=True))).run("pre_tool",
                                     HookContext(point="pre_tool"))
    assert exc.value.reported is True
    assert exc.value.response.summary == "done"
