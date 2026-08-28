"""tests/test_agent/test_multi.py —— M15 §5 验收：子代理隔离 / 冲突分组 / 并行 / 预算 / A2A。

四个验收单测（§5）：
- test_subagent_isolated_context        隔离：任务书里没有的信息不进子代理上下文与报告
- test_write_conflict_serialized        写目标相交 → 同组串行
- test_parallel_wallclock               无依赖子任务并行 → 墙钟 < 单串之和
- test_budget_exceeded_returns_report   预算耗尽 → 返回报告而非挂死

补充覆盖（§2 问答 / §4 步骤）：
- 拆解 JSON 解析 + 聚合 usage 汇总 + 冲突检测
- 内置三角色的工具白名单（explorer/verifier 物理上无写工具）
- 角色卡 markdown 解析（from_markdown）
- A2A：card 发现（含缓存失效）/ send / poll / input-required 适配 / 远程工人进编排
- MultiStrategy 接线（M13 骨架 → M15 并行）
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from pydantic import BaseModel

from agent_godot.agent import (AgentLoop, Dispatcher, EventBus, Orchestrator,
                               Session, SubagentSpec, Subtask, SubtaskResult,
                               spawn)
from agent_godot.agent.a2a import A2AClient, AgentCard
from agent_godot.agent.paradigms import MultiStrategy
from agent_godot.agent.subagents.base import Budget as SpecBudget
from agent_godot.agent.subagents.builtin import (build_default_specs,
                                                 describe_specs)
from agent_godot.core import Message, ToolCall, Usage
from agent_godot.tools import BaseTool, ToolMeta, ToolRegistry, ToolResponse

from .conftest import FakeLLM, done_ev, text_ev


# ---------- 测试工具与假件 ----------

class _Tool(BaseTool):
    """测试用文件工具（readonly 由 _attach 指定）。"""

    meta = None

    class Params(BaseModel):
        path: str = "x.gd"
        content: str = ""

    async def run(self, path: str = "x.gd", content: str = "") -> ToolResponse:
        return ToolResponse(ok=True, summary=f"wrote {path}")


def _attach(name: str, readonly: bool, tags: set[str] | None = None):
    inst = _Tool()
    inst.meta = ToolMeta(name=name, description=name, readonly=readonly,
                         tags=tags or {"fs"})
    return inst


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_attach("read_file", True))
    reg.register(_attach("write_file", False))
    reg.register(_attach("list_files", True))
    return reg


class RecordingLLM:
    """记录每次请求的假模型（隔离断言要看它收到的 messages）。"""

    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.requests: list = []

    async def stream(self, req):
        self.requests.append(req)
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for ev in self.script[idx]:
            yield ev


class PlannerLLM:
    """返回预置拆解 JSON 的假编排器（只实现 complete）。"""

    def __init__(self, payload):
        self.payload = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False)

    async def complete(self, req):
        class _R:
            content = self.payload
        return _R()


def remote_spec(name: str, *, report: str = "完成", delay: float = 0.0,
                artifacts: tuple[str, ...] = (), ok: bool = True,
                stop: str = "natural") -> SubagentSpec:
    """远程假工人（省掉子代理内部 Loop，专测编排层并发与聚合）。"""

    async def _run(task: str, ctx: dict) -> SubtaskResult:
        if delay:
            await asyncio.sleep(delay)
        return SubtaskResult(spec_name=name, ok=ok, report=report,
                             artifacts=list(artifacts),
                             usage=Usage(10, 5, 0.001), stop_reason=stop)

    return SubagentSpec(name=name, role_prompt=f"{name} 角色",
                        tools=ToolRegistry(), model="fake", remote=_run)


def _orch(specs: dict[str, SubagentSpec], llm=None, **kw) -> Orchestrator:
    return Orchestrator(llm=llm or PlannerLLM([]), specs=specs,
                        registry=_registry(), **kw)


# ---------- ① 隔离：任务书里没有的信息不得出现在子代理上下文/报告中 ----------

async def test_subagent_isolated_context():
    """子代理看不到主控对话（上下文隔离），产物里也不得泄露未注入的信息。"""
    reg = _registry()
    llm = RecordingLLM([[text_ev("勘察报告：save/ 目录 3 个文件"),
                         done_ev(None, "stop")]])
    spec = SubagentSpec(name="explorer",
                        role_prompt="你是代码勘察员，只读不写。",
                        tools=reg.filter(readonly=True), model="fake")

    main = Session("main")
    main.append(Message(role="user",
                        content="主控秘密：绝不外传的主控密钥 SECRET-123"))
    before = len(main.messages)

    result = await spawn(spec, "勘察 save/ 目录", {"llm": llm, "model": "fake"})

    assert result.ok and "勘察报告" in result.report
    # ① 主控上下文信息没有进入子代理的请求（隔离是双向的）
    sent = " ".join((m.content or "")
                    for req in llm.requests for m in req.messages)
    assert "SECRET-123" not in sent
    # ② 主控会话没有被子代理的过程消息污染
    assert len(main.messages) == before
    # ③ 回传物只有报告（结构化 SubtaskResult，不含消息列表）
    assert not hasattr(result, "messages")


# ---------- ② 写目标冲突 → 同组串行 ----------

async def test_write_conflict_serialized():
    """两个子任务写同一个文件 → 必须同组（组内串行），不能并行写。"""
    orch = _orch({"coder": remote_spec("coder")})
    subs = [Subtask(title="a", task_brief="改 a.gd", write_targets={"a.gd"}),
            Subtask(title="b", task_brief="也改 a.gd", write_targets={"a.gd"})]
    groups = orch.resolve_groups(subs)
    assert len(groups) == 1, "写目标相交必须并进同一组排队"
    assert [s.title for s in groups[0]] == ["a", "b"]


async def test_independent_subtasks_parallel_and_depends_merged():
    """写目标不相交 → 各自成组（并行）；depends → 与依赖合并成串行链。"""
    orch = _orch({"coder": remote_spec("coder")})
    subs = [Subtask(title="a", write_targets={"a.gd"}),
            Subtask(title="b", write_targets={"b.gd"}),
            Subtask(title="c", depends=["a"], write_targets={"c.gd"})]
    groups = orch.resolve_groups(subs)
    assert len(groups) == 2
    assert [s.title for s in groups[0]] == ["a", "c"], "依赖链必须同组串行"
    assert [s.title for s in groups[1]] == ["b"], "无依赖者保持并行"


async def test_depends_across_groups_merges_them():
    """c 依赖 a 与 b（分属两组）→ 两组合并，保证 c 在两个前驱都跑完后才跑。"""
    orch = _orch({"coder": remote_spec("coder")})
    subs = [Subtask(title="a", write_targets={"a.gd"}),
            Subtask(title="b", write_targets={"b.gd"}),
            Subtask(title="c", depends=["a", "b"], write_targets={"c.gd"})]
    groups = orch.resolve_groups(subs)
    assert len(groups) == 1
    assert [s.title for s in groups[0]] == ["a", "b", "c"]


# ---------- ③ 并行墙钟 ----------

async def test_parallel_wallclock():
    """两个 0.3s 的独立子任务并发 → 总耗时接近 0.3s（而非 0.6s）。"""
    specs = {"coder": remote_spec("coder", delay=0.3, report="A 完成"),
             "verifier": remote_spec("verifier", delay=0.3, report="B 完成")}
    orch = _orch(specs, max_parallel=3)
    subs = [Subtask(title="A", task_brief="做 A", spec_name="coder",
                    write_targets={"a.gd"}),
            Subtask(title="B", task_brief="做 B", spec_name="verifier")]

    t0 = time.monotonic()
    out = await orch.run(Session("s"), "加存档系统", subtasks=subs)
    elapsed = time.monotonic() - t0

    assert out.steps == 2
    assert elapsed < 0.55, f"并行失效：两路各 0.3s 却花了 {elapsed:.2f}s"
    assert all(r.ok for r in out.results)


async def test_conflict_group_runs_serially():
    """同组（写目标冲突）串行执行：墙钟 ≈ 两个之和（这是正确的保守行为）。"""
    specs = {"coder": remote_spec("coder", delay=0.2)}
    orch = _orch(specs, max_parallel=3)
    subs = [Subtask(title="a", task_brief="改 a", spec_name="coder",
                    write_targets={"a.gd"}),
            Subtask(title="b", task_brief="也改 a", spec_name="coder",
                    write_targets={"a.gd"})]
    t0 = time.monotonic()
    await orch.run(Session("s"), "任务", subtasks=subs)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.4, f"同组不该并发（实测 {elapsed:.2f}s）"


# ---------- ④ 预算耗尽 → 返回报告而非挂死 ----------

async def test_budget_exceeded_returns_report():
    """steps=1 的子代理跑"永远调工具"的任务 → 优雅收尾，返回 max_steps 报告。"""
    reg = _registry()
    # 剧本：每轮都调工具（无 stop），收尾轮只取 text_delta
    llm = FakeLLM([[text_ev("进展：已改两个文件"),
                    done_ev([ToolCall(id="c1", name="write_file",
                                      arguments='{"path": "save.gd"}')],
                            "tool_calls")]])
    spec = SubagentSpec(name="coder", role_prompt="你是实现者。", tools=reg,
                        model="fake", budget=SpecBudget(steps=1, tokens=1000))

    result = await spawn(spec, "无限任务", {"llm": llm, "model": "fake"})

    assert result.stop_reason == "max_steps"
    assert result.ok is False, "预算耗尽不算成功交付"
    assert "进展" in result.report, "必须带回已完成部分的报告，而不是空手而归"
    assert result.artifacts == ["save.gd"], "产出物清单照常回收（聚合查冲突用）"


# ---------- ⑤ 拆解 / 聚合 / 冲突 ----------

async def test_decompose_parses_json_and_falls_back():
    """拆解 JSON → Subtask；模型吐不出 JSON → 降级为单 coder 子任务。"""
    payload = [
        {"title": "勘察", "brief": "摸清 save/ 目录", "spec": "explorer",
         "write_targets": [], "depends": []},
        {"title": "实现", "brief": "写 save_manager.gd，验收：headless 通过",
         "spec": "coder", "write_targets": ["save_manager.gd"],
         "depends": ["勘察"]},
    ]
    orch = _orch({"coder": remote_spec("coder")}, llm=PlannerLLM(payload))
    subs = await orch.decompose("加存档系统")

    assert [s.title for s in subs] == ["勘察", "实现"]
    assert subs[1].write_targets == {"save_manager.gd"}
    assert subs[1].depends == ["勘察"]

    bad = _orch({"coder": remote_spec("coder")}, llm=PlannerLLM("我不是 JSON"))
    fallback = await bad.decompose("加存档系统")
    assert len(fallback) == 1 and fallback[0].spec_name == "coder", \
        "拆解失败必须降级为单子任务（multi 永远可用）"


async def test_aggregate_sums_usage_and_detects_conflicts():
    """聚合：usage 逐项汇总；两个子代理产出同一文件 → 冲突被显式列出。"""
    specs = {"coder": remote_spec("coder", artifacts=("save_manager.gd",)),
             "verifier": remote_spec("verifier", artifacts=("save_manager.gd",)),
             "explorer": remote_spec("explorer")}
    orch = _orch(specs, auto_arbitrate=False)
    results = [
        SubtaskResult(spec_name="coder", ok=True, title="实现",
                      report="已实现", artifacts=["save_manager.gd"],
                      usage=Usage(100, 50, 0.01)),
        SubtaskResult(spec_name="verifier", ok=True, title="验收",
                      report="通过", artifacts=["save_manager.gd"],
                      usage=Usage(60, 40, 0.005)),
        SubtaskResult(spec_name="explorer", ok=False, title="勘察",
                      report="", usage=Usage(10, 5, 0.0),
                      stop_reason="max_steps"),
    ]
    out = await orch.aggregate(results, task="加存档")

    assert out.usage.input_tokens == 170 and out.usage.output_tokens == 95
    assert abs(out.usage.cost_usd - 0.015) < 1e-6
    assert any("save_manager.gd" in c for c in out.conflicts), "产出物冲突要报"
    assert any("未产出交付报告" in c for c in out.conflicts)
    assert len(out.conflicts) == 2, f"冲突不该重复计数: {out.conflicts}"
    assert out.stop_reason == "partial"


async def test_aggregate_arbitrates_conflicts_with_verifier():
    """有冲突 → 自动派 verifier 仲裁（只给意见不修文件），意见进交付报告。"""
    specs = {"coder": remote_spec("coder"),
             "verifier": remote_spec("verifier",
                                     report="建议保留实现版，删除重复文件")}
    orch = _orch(specs)                        # auto_arbitrate 默认开
    results = [
        SubtaskResult(spec_name="coder", ok=True, title="实现",
                      report="已实现", artifacts=["a.gd"],
                      usage=Usage(10, 5, 0.0)),
        SubtaskResult(spec_name="coder", ok=True, title="另一个实现",
                      report="也实现了", artifacts=["a.gd"],
                      usage=Usage(10, 5, 0.0)),
    ]
    out = await orch.aggregate(results, task="加存档")
    assert len(out.conflicts) == 1
    assert "保留" in out.arbitration, "仲裁意见必须进交付报告"
    assert "仲裁意见" in out.report


async def test_failed_subtask_is_retried_once():
    """预算类失败（max_steps）→ 改任务书重派一次；仍失败则进冲突清单。"""
    calls: list[str] = []

    def make():
        async def _run(task: str, ctx: dict) -> SubtaskResult:
            calls.append(task)
            return SubtaskResult(spec_name="coder", ok=False, report="中断",
                                 stop_reason="max_steps")
        return _run

    spec = SubagentSpec(name="coder", role_prompt="实现者", tools=ToolRegistry(),
                        model="fake", remote=make())
    orch = _orch({"coder": spec}, auto_arbitrate=False)
    out = await orch.run(Session("s"), "任务",
                         subtasks=[Subtask(title="实现", task_brief="写代码",
                                           spec_name="coder")])
    assert len(calls) == 2, "预算类失败应重派一次"
    assert "重派说明" in calls[1], "重派要带上中断上下文（不是原样重跑）"
    assert out.results[0].attempts == 2


# ---------- ⑥ 内置角色白名单与角色卡 ----------

def test_builtin_specs_tool_whitelist():
    """explorer/verifier 物理上拿不到写工具；coder 有写工具（白名单=能力边界）。"""
    specs = build_default_specs(_registry())
    assert set(specs) == {"explorer", "coder", "verifier"}
    assert "write_file" not in specs["explorer"].tools.names()
    assert "write_file" not in specs["verifier"].tools.names()
    assert "write_file" in specs["coder"].tools.names()
    # 按角色配模型：探查/验收廉价档，实现推理档
    assert specs["explorer"].model != specs["coder"].model
    assert specs["explorer"].budget.steps < specs["coder"].budget.steps
    assert "explorer" in describe_specs(specs)


def test_spec_from_markdown(tmp_path):
    """角色卡：frontmatter（name/model/tools/steps）+ 正文当 role_prompt。"""
    card = tmp_path / "reviewer.md"
    card.write_text(
        "---\nname: reviewer\ndescription: 代码评审\nmodel: deepseek/deepseek-chat\n"
        "tools: [read_file, fs]\nreadonly: true\nsteps: 5\ntokens: 9000\n---\n"
        "你是代码评审员，只评审不修改。", encoding="utf-8")
    spec = SubagentSpec.from_markdown(card, _registry())

    assert spec.name == "reviewer"
    assert spec.model == "deepseek/deepseek-chat"
    assert "read_file" in spec.tools.names()
    assert "write_file" not in spec.tools.names(), "readonly=true 必须过滤掉写工具"
    assert spec.budget.steps == 5 and spec.budget.tokens == 9000
    assert spec.role_prompt == "你是代码评审员，只评审不修改。"


# ---------- ⑦ A2A ----------

class _Resp:
    def __init__(self, data: dict):
        self._data = data

    def json(self):
        return self._data


class FakeTransport:
    """A2A 假传输：按调用顺序回放响应（get=名片，post=send→poll）。

    剧本放完后再被调用时，重复最后一次响应（真实服务端对同一个 taskId
    的重复 get 也是幂等的）。
    """

    def __init__(self, card: dict, posts: list[dict]):
        self.card = card
        self.posts = list(posts)
        self.calls: list[dict] = []
        self.gets = 0
        self.last = {"result": {"id": "t-0", "status": {"state": "completed"}}}

    async def get(self, url, **kw):
        self.gets += 1
        return _Resp(self.card)

    async def post(self, url, json=None, headers=None):
        self.calls.append(json or {})
        if self.posts:
            self.last = self.posts.pop(0)
        return _Resp(self.last)


_CARD = {"name": "godot-asset-agent", "version": "1.2.0",
         "description": "Godot 资产市场专家",
         "url": "https://a2a.example.com/rpc",
         "skills": [{"id": "asset", "name": "资产检索"}],
         "authentication": {"schemes": ["bearer"], "credentials": "TOKEN_ENV"}}


def _completed(text: str = "已找到 3 个可用资产") -> dict:
    return {"result": {"id": "t-1", "status": {"state": "completed"},
                       "artifacts": [{"name": "assets.json", "parts": [
                           {"kind": "text", "text": text}]}]}}


async def test_a2a_discover_send_poll_roundtrip():
    """card 发现 → send 拿 taskId → poll 到 completed → 包装成子代理跑一轮。"""
    transport = FakeTransport(_CARD, [
        {"result": {"id": "t-1", "status": {"state": "working"}}},
        _completed()])
    client = A2AClient(transport=transport, poll_interval=0.01)

    card = await client.discover("https://a2a.example.com")
    assert card.name == "godot-asset-agent" and card.version == "1.2.0"
    assert card.endpoint == "https://a2a.example.com/rpc"
    assert card.skills == ["资产检索"]

    task = await client.run_task(card, "找三个 CC0 的 Godot 存档图标")
    assert task.ok and task.id == "t-1"
    assert "已找到 3 个可用资产" in task.text
    assert task.artifacts == ["assets.json"]

    worker = client.as_remote_worker(card)
    assert worker.is_remote
    result = await spawn(worker, "找图标", {})
    assert result.ok and result.stop_reason == "natural"
    assert "已找到" in result.report


async def test_a2a_card_cache_ttl_and_invalidation():
    """名片缓存：TTL 内复用（省一次往返）；force / 过期 → 重发现（能力漂移检测）。"""
    transport = FakeTransport(_CARD, [])
    client = A2AClient(transport=transport)          # 默认 TTL=300s
    await client.discover("https://a2a.example.com")
    await client.discover("https://a2a.example.com")
    assert transport.gets == 1, "TTL 内应命中缓存"

    await client.discover("https://a2a.example.com", force=True)
    assert transport.gets == 2, "force 必须重发现"

    transport.card = {**_CARD, "version": "2.0.0"}
    short = A2AClient(transport=transport, cache_ttl=0)   # TTL=0：每次重发现
    card = await short.discover("https://a2a.example.com")
    assert card.version == "2.0.0", "对方能力变了必须能拿到新名片"


async def test_a2a_input_required_maps_to_confirm_gate():
    """input-required（远程挂起）→ 本地确认门事件（协议适配归 adapter）。"""
    transport = FakeTransport(_CARD, [
        {"result": {"id": "t-2", "status": {"state": "working"}}},
        {"result": {"id": "t-2", "status": {"state": "input-required",
                                            "message": {"parts": [
                                                {"kind": "text",
                                                 "text": "要 JSON 还是 ConfigFile？"}]}}}}])
    bus = EventBus()
    client = A2AClient(transport=transport, poll_interval=0.01, bus=bus)
    card = AgentCard.from_json(_CARD, base_url="https://a2a.example.com")

    events = []
    consumer = asyncio.create_task(_collect(bus, events))
    result = await spawn(client.as_remote_worker(card), "做存档", {"bus": bus})
    await bus.close()
    await consumer

    assert result.stop_reason == "input_required"
    assert result.ok is False
    assert "需要补充信息" in result.report
    assert any(e.type == "a2a_input_required" for e in events), \
        "远程挂起必须转成主控能看见的确认事件"


async def test_orchestrator_can_dispatch_remote_worker():
    """A2A 远程工人进了编排管线后，与本地子代理同构（都吐 SubtaskResult）。"""
    transport = FakeTransport(_CARD, [
        {"result": {"id": "t-3", "status": {"state": "completed"}}},
        _completed("外包交付：3 个资产")])
    client = A2AClient(transport=transport, poll_interval=0.01)
    card = AgentCard.from_json(_CARD, base_url="https://a2a.example.com")

    orch = _orch({"coder": remote_spec("coder", report="本地实现完成"),
                  "a2a:godot-asset-agent": client.as_remote_worker(card)},
                 auto_arbitrate=False)
    out = await orch.run(Session("s"), "做存档 + 配资产", subtasks=[
        Subtask(title="本地实现", task_brief="写 save_manager.gd",
                spec_name="coder", write_targets={"save_manager.gd"}),
        Subtask(title="外包资产", task_brief="找 3 个图标",
                spec_name="a2a:godot-asset-agent")])
    assert out.steps == 2
    assert any("外包交付" in r.report for r in out.results)


# ---------- ⑧ M13 ↔ M15 接线 ----------

async def test_multi_strategy_drives_orchestrator():
    """MultiStrategy.run_multi_mode → Orchestrator（M13 骨架长出血肉）。"""
    loop = AgentLoop(FakeLLM([[text_ev("x"), done_ev(None, "stop")]]),
                     Dispatcher(_registry()), model="fake")
    strategy = MultiStrategy(
        llm=PlannerLLM([{"title": "勘察", "brief": "看目录", "spec": "explorer"},
                        {"title": "实现", "brief": "写代码", "spec": "coder",
                         "write_targets": ["save.gd"]}]),
        loop=loop, max_parallel=2,
        specs={"explorer": remote_spec("explorer", report="3 个文件"),
               "coder": remote_spec("coder", report="已实现")})

    out = await strategy.run_multi_mode(Session("s"), "加存档系统")
    assert out.steps == 2 and out.ok
    assert len(out.groups) == 2, "无写目标冲突 → 两路并行"
    assert "已实现" in out.report

    with pytest.raises(RuntimeError):
        MultiStrategy(llm=None, loop=None).build_orchestrator()


async def _collect(bus: EventBus, sink: list) -> None:
    async for ev in bus.stream():
        sink.append(ev)
