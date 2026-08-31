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
import os
import time

import pytest
from pydantic import BaseModel

from agent_godot.agent import (AgentLoop, Dispatcher, EventBus, Orchestrator,
                               Session, SubagentSpec, Subtask, SubtaskResult,
                               spawn)
from agent_godot.agent.a2a import A2AClient, AgentCard
from agent_godot.agent.orchestrator import (Access, Conflict, WriteScope,
                                            load_protected)
from agent_godot.agent.paradigms import MultiStrategy
from agent_godot.agent.subagents.base import (Constraints, Rule,
                                              _extract_assumptions,
                                              load_constraints)
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


def _sub(title: str, targets, *, spec_name: str = "coder", **kw) -> Subtask:
    """建工单。targets 三态（§1.4 ①-4）：None=未声明 / set()=声明只读 / 集合=声明写。

    默认 spec 是 `remote_spec`（is_remote=True）→ 不会被判成"物理只读"，
    所以 `None` 会如实走到 fail-safe 分支。
    """
    return Subtask(title=title, task_brief=f"{title} 的任务书",
                   spec_name=spec_name, write_targets=targets, **kw)


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
    """两个子任务写同一个文件 → 不许并行（决策树 #2：重叠度 1.0 → 合并成一个）。"""
    orch = _orch({"coder": remote_spec("coder")})
    subs = [_sub("a", {"a.gd"}), _sub("b", {"a.gd"})]
    groups = orch.resolve_groups(subs)
    assert len(groups) == 1, "写目标相交绝不能拆成两组并行"
    assert sum(len(g) for g in groups) == 1, "重叠度>0.5 → 合并成一个子任务"


async def test_partial_overlap_stays_two_serial_subtasks():
    """重叠度 ≤0.5（还能各自独立干活）→ 串行排队而不是合并。"""
    orch = _orch({"coder": remote_spec("coder")})
    subs = [_sub("a", {"a.gd", "b.gd"}), _sub("b", {"a.gd", "c.gd"})]
    groups = orch.resolve_groups(subs)
    assert len(groups) == 1 and len(groups[0]) == 2, "部分重叠 → 同组串行但不合并"
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
    """两个 0.3s 的子任务并发 → 总耗时接近 0.3s（而非 0.6s）。

    B 声明 `write_targets=set()`（**声明只读**）——三态里 `[]` 才表示"我不写
    任何文件"，从而与 A 的写目标并行；写 None 是"未声明"，会被 fail-safe 串行化。
    """
    specs = {"coder": remote_spec("coder", delay=0.3, report="A 完成"),
             "verifier": remote_spec("verifier", delay=0.3, report="B 完成")}
    orch = _orch(specs, max_parallel=3)
    subs = [_sub("A", {"a.gd"}, spec_name="coder"),
            _sub("B", set(), spec_name="verifier")]

    t0 = time.monotonic()
    out = await orch.run(Session("s"), "加存档系统", subtasks=subs)
    elapsed = time.monotonic() - t0

    assert out.steps == 2
    assert elapsed < 0.55, f"并行失效：两路各 0.3s 却花了 {elapsed:.2f}s"
    assert all(r.ok for r in out.results)


async def test_conflict_group_runs_serially():
    """同组（写目标部分重叠 → 串行档）串行执行：墙钟 ≈ 两者之和。

    直接测 `_run_group`（走 `run` 会先过健康检查：冲突导致的单组会被判定为
    "白拆"并降级——那是体检的用例，不是本用例的被测行为）。
    """
    specs = {"coder": remote_spec("coder", delay=0.2)}
    orch = _orch(specs, max_parallel=3)
    t0 = time.monotonic()
    results = await orch._run_group(
        [_sub("a", {"a.gd", "b.gd"}), _sub("b", {"a.gd", "c.gd"})], {})
    elapsed = time.monotonic() - t0
    assert len(results) == 2
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
        # 外包工人只回资产清单、不落项目文件 → 声明为只读（set()）
        Subtask(title="外包资产", task_brief="找 3 个图标",
                spec_name="a2a:godot-asset-agent", write_targets=set())])
    assert out.steps == 2
    assert any("外包交付" in r.report for r in out.results)


# ---------- ⑧ M13 ↔ M15 接线 ----------

async def test_multi_strategy_drives_orchestrator():
    """MultiStrategy.run_multi_mode → Orchestrator（M13 骨架长出血肉）。"""
    loop = AgentLoop(FakeLLM([[text_ev("x"), done_ev(None, "stop")]]),
                     Dispatcher(_registry()), model="fake")
    strategy = MultiStrategy(
        llm=PlannerLLM([{"title": "勘察", "brief": "看目录", "spec": "explorer",
                         "write_targets": []},
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


# =========================================================================
# ⑨ §1.4 静态冲突检查：判定精度（第二轮加固）
# =========================================================================

async def test_dir_prefix_conflict():
    """目录 ⊇ 文件 必须判为冲突（前缀包含，不是字符串相等）。

    这是 §6 踩坑记录 2026-08-30 第一条的回归测试：精确字符串相等会把
    `save/` 与 `save/manager.gd` 判成不冲突 → 两组并行 → 后写覆盖前写。
    """
    orch = _orch({"coder": remote_spec("coder")})
    subs = [_sub("A", {"save/"}), _sub("B", {"save/manager.gd"})]
    assert orch.detect_conflicts(subs), "目录与文件是同一棵前缀树的父子节点"


async def test_path_alias_conflict():
    """同一文件的多种写法必须归一化到同一节点（win32 分隔符 + res:// 前缀）。"""
    orch = _orch({"coder": remote_spec("coder")})
    subs = [_sub("A", {".\\src\\a.gd"}), _sub("B", {"res://src/a.gd"})]
    hits = orch.detect_conflicts(subs)
    assert hits and hits[0].kind == "write_write"
    assert hits[0].scope == "src/a.gd", "两种写法必须归一化成同一个节点"


async def test_no_false_conflict_on_similar_prefix():
    """归一化不能过度：`save/` 不该吃掉 `savesettings.gd`（目录补尾斜杠的意义）。"""
    orch = _orch({"coder": remote_spec("coder")})
    subs = [_sub("A", {"save/"}), _sub("B", {"savesettings.gd"})]
    assert not orch.detect_conflicts(subs)


def test_case_folding_follows_filesystem():
    """大小写折叠策略由调用方按文件系统语义给定（NTFS 不敏感 / ext4 敏感）。"""
    assert WriteScope.of("Save/Manager.gd", case_insensitive=True).norm == \
        "save/manager.gd"
    assert WriteScope.of("Save/Manager.gd", case_insensitive=False).norm == \
        "Save/Manager.gd"
    # 默认跟随 os.name：同一份编排代码跨平台判定结果必须一致
    orch = _orch({"coder": remote_spec("coder")})
    assert orch.case_insensitive == (os.name == "nt")


def test_access_tristate():
    """访问三态：READ 与任何访问都不冲突（读是安全的）；EXCLUSIVE 与写冲突。

    注意 READ 判定在最前面：`conflicts_with` 只要一方是 READ 就短路返回 False
    ——读受保护文件本身无害，所以 EXCLUSIVE × READ 也不算冲突（§1.4 ③(a) 的
    判定顺序：先 READ、后 EXCLUSIVE）。
    """
    w = WriteScope.of("a.gd", Access.WRITE)
    r = WriteScope.of("a.gd", Access.READ)
    x = WriteScope.of("a.gd", Access.EXCLUSIVE)
    assert not w.conflicts_with(r) and not r.conflicts_with(r)
    assert not x.conflicts_with(r), "读受保护文件无害"
    assert w.conflicts_with(w) and x.conflicts_with(w)


async def test_write_read_becomes_data_dependency():
    """写-读不并行也不报错：升级为 depends（串行 + 前驱产出注入，处置 #5）。"""
    orch = _orch({"coder": remote_spec("coder"),
                  "verifier": remote_spec("verifier")})
    subs = [_sub("写", {"a.gd"}),
            _sub("读", {"a.gd"}, spec_name="verifier", access=Access.READ)]
    groups = orch.resolve_groups(subs)
    assert len(groups) == 1, "读者必须等写者写完（否则读到旧内容）"
    assert [s.title for s in groups[0]] == ["写", "读"]


async def test_read_read_stays_parallel():
    """读-读天然并行（处置 #6）。"""
    orch = _orch({"verifier": remote_spec("verifier")})
    subs = [_sub("读1", {"a.gd"}, spec_name="verifier", access=Access.READ),
            _sub("读2", {"a.gd"}, spec_name="verifier", access=Access.READ)]
    assert len(orch.resolve_groups(subs)) == 2


async def test_undeclared_is_fail_safe():
    """未声明（None）≠ 只读（[]）：必须与所有写着同组串行 + 记告警。

    这是 §1.4 ⑤-3 的回归测试：LLM 漏输出 write_targets → 空集与谁都不相交
    → 静默并行写同一文件。把"未知"当"无"是 fail-open。
    """
    orch = _orch({"coder": remote_spec("coder")})
    subs = [_sub("A", {"a.gd"}), _sub("B", None)]
    assert len(orch.resolve_groups(subs)) == 1
    assert ("undeclared_write_targets", "B") in orch.warnings


async def test_physically_readonly_role_undeclared_is_safe():
    """工具白名单里一个写工具都没有 → 未声明可按"声明只读"处理（不是 fail-open）。

    这是白名单给出的**硬结论**而非猜测：没有写工具就不可能写文件。只对
    "有可能写"的角色（有写工具 / 远程工人）才走 fail-safe。
    """
    reg = _registry()
    ro = SubagentSpec(name="reader", role_prompt="只读",
                      tools=reg.filter(readonly=True), model="fake")
    rw = SubagentSpec(name="writer", role_prompt="可写", tools=reg, model="fake")
    orch = _orch({"reader": ro, "writer": rw})
    subs = [_sub("写", {"a.gd"}, spec_name="writer"),
            _sub("读", None, spec_name="reader")]
    assert len(orch.resolve_groups(subs)) == 2
    assert not orch.warnings, "物理只读不该记未声明告警"


async def test_decompose_missing_write_targets_is_undeclared():
    """拆解 JSON 缺 write_targets 字段 → None（未声明），不是 set()。"""
    orch = _orch({"coder": remote_spec("coder")},
                 llm=PlannerLLM([{"title": "A", "brief": "改 a.gd",
                                  "spec": "coder"}]))
    subs = await orch.decompose("任务")
    assert subs[0].write_targets is None, "字段缺失 = 未声明（fail-safe 输入）"

    orch2 = _orch({"coder": remote_spec("coder")},
                  llm=PlannerLLM([{"title": "A", "brief": "看 a.gd",
                                   "spec": "coder", "write_targets": []}]))
    assert (await orch2.decompose("任务"))[0].write_targets == set()


# ---------- ⑨-b 处置决策树 ----------

async def test_decision_tree_routes_by_kind():
    """决策树按 kind 打 action：protected→上抛 / undeclared→串行 / 写读→依赖。"""
    orch = _orch({"coder": remote_spec("coder")}, protected=["project.godot"])
    conflicts = [
        Conflict("A", "B", "project.godot", "protected"),
        Conflict("C", "D", "*", "undeclared"),
        Conflict("E", "F", "a.gd", "write_read"),
        Conflict("G", "H", "b.gd", "write_write"),
    ]
    out = orch.plan_conflicts(conflicts)
    assert [c.action for c in out] == ["escalate", "serialize", "depends",
                                       "serialize"]
    assert ("undeclared_write_targets", "C") in orch.warnings


async def test_contract_is_opt_in_only():
    """contract 是显式开关：默认关闭（保守档），开启后也要有明确接缝才切。"""
    orch = _orch({"coder": remote_spec("coder")})
    subs = [_sub("A", {"save_manager.gd"}), _sub("B", {"save_manager.gd"})]
    orch._index(subs)
    # 重叠度 1.0（同一个文件）→ 默认走 merge，不是 contract
    assert orch.plan_conflicts([Conflict("A", "B", "save_manager.gd",
                                         "write_write")])[0].action == "merge"

    orch.allow_contract = True                  # 开了开关但没声明接缝 → 仍不切
    assert orch.plan_conflicts([Conflict("A", "B", "save_manager.gd",
                                         "write_write")])[0].action == "merge"
    subs[0].seam = subs[1].seam = "save_types.gd"
    subs[0].seam_target, subs[1].seam_target = "save_config.gd", "save_codec.gd"
    assert orch._has_seam(Conflict("A", "B", "save_manager.gd", "write_write"))

    groups = orch.resolve_groups(subs)          # 契约步骤 + 两路并行
    titles = [[s.title for s in g] for g in groups]
    assert any("起草契约" in t for g in titles for t in g)
    assert sum(1 for g in groups if g[0].title in ("A", "B")) == 2, "并行度 1 → 2"


async def test_contract_barrier_runs_implementations_in_parallel():
    """契约步骤是**屏障**：起草完契约后，两路实现才并行开工（墙钟 = 单路）。"""
    def make(delay: float):
        async def _run(task: str, ctx: dict) -> SubtaskResult:
            await asyncio.sleep(delay)
            return SubtaskResult(spec_name="coder", ok=True, report="完成")
        return _run

    orch = _orch({"coder": SubagentSpec(
        name="coder", role_prompt="实现", tools=ToolRegistry(), model="fake",
        remote=make(0.2))}, allow_contract=True, max_parallel=3)
    subs = [_sub("A", {"save_manager.gd"}), _sub("B", {"save_manager.gd"})]
    subs[0].seam = subs[1].seam = "save_types.gd"
    subs[0].seam_target, subs[1].seam_target = "save_config.gd", "save_codec.gd"

    t0 = time.monotonic()
    out = await orch.run(Session("s"), "加存档", subtasks=subs)
    elapsed = time.monotonic() - t0
    # 契约(0.2) → A/B 并行(0.2)：约 0.4s；若屏障失效（三者同组串行）则 ≈0.6s
    assert elapsed < 0.55, f"屏障后两路应并行（实测 {elapsed:.2f}s）"
    assert out.steps == 3, "契约步骤 + 两路实现"


# ---------- ⑨-c 串行链：产出注入 + fail-fast ----------

async def test_chain_injects_predecessor_output():
    """串行链上后继必须拿到前驱的报告摘要 + 变更文件 hash（防语义覆盖）。"""
    orch = _orch({"coder": remote_spec("coder")})
    done = SubtaskResult("coder", True, "已写入 a.gd", artifacts=["a.gd"],
                         title="A", file_hashes={"a.gd": "deadbeef"})
    hint = orch._chain_ctx({}, done)["chain_hint"]
    assert "A" in hint and "a.gd" in hint and "增量" in hint
    assert "deadbeef" in hint, "基线 hash 要传下去（让后继增量改、且乐观锁一次命中）"


async def test_upstream_failure_blocks_downstream():
    """前驱失败 → 后继标 blocked 跳过，不许在半成品上继续写。"""
    orch = _orch({"coder": remote_spec("coder", ok=False, stop="max_steps")})
    results = await orch._run_group([_sub("A", {"a.gd"}), _sub("B", {"a.gd"})], {})
    assert len(results) == 2
    assert results[0].stop_reason == "max_steps"
    assert results[1].stop_reason == "blocked"
    assert "阻塞" in results[1].report


async def test_tolerate_upstream_failure_keeps_going():
    """工单显式声明 tolerate_upstream_failure → 前驱失败也照跑（用户知情的选择）。"""
    orch = _orch({"coder": remote_spec("coder", ok=False, stop="error")})
    results = await orch._run_group(
        [_sub("A", {"a.gd"}),
         _sub("B", {"a.gd"}, tolerate_upstream_failure=True)], {})
    assert all(r.stop_reason != "blocked" for r in results)


async def test_chain_hint_reaches_successor_brief():
    """注入的 chain_hint 必须真的进到后继的任务书里（不是只存在 ctx 里）。"""
    seen: list[str] = []

    def make():
        async def _run(task: str, ctx: dict) -> SubtaskResult:
            seen.append(ctx.get("chain_hint") or "")
            return SubtaskResult(spec_name="coder", ok=True, report="改好了 a.gd")
        return _run

    spec = SubagentSpec(name="coder", role_prompt="实现", tools=ToolRegistry(),
                        model="fake", remote=make())
    orch = _orch({"coder": spec}, max_retries=0)
    await orch._run_group([_sub("A", {"a.gd"}), _sub("B", {"a.gd"})], {})
    assert seen[0] == "", "第一个子任务没有前驱"
    assert "增量" in seen[1], "第二个子任务必须看到前驱的产出摘要"


# ---------- ⑨-d 确定性与体检 ----------

async def test_grouping_is_deterministic():
    """稳定排序：同输入两次分组结果必须完全一致（可复现是测试对拍的前提）。"""
    orch = _orch({"coder": remote_spec("coder")})
    subs = [_sub("C", {"c.gd"}), _sub("A", {"a.gd"}), _sub("B", {"b.gd"})]
    first = [[s.title for s in g] for g in orch.resolve_groups(subs)]
    second = [[s.title for s in g] for g in orch.resolve_groups(subs)]
    assert first == second == [["A"], ["B"], ["C"]], "按 title 字典序而非输入顺序"

    conflicted = [_sub("C", {"c.gd"}), _sub("A", {"a.gd"}), _sub("B", {"a.gd"})]
    assert orch.resolve_groups(conflicted) == orch.resolve_groups(conflicted)


async def test_parallelism_health_check():
    """分组后仅 1 组 = 白拆：带 hint 重拆一次，仍为 1 则降级单 coder。"""
    orch = _orch({"coder": remote_spec("coder")},
                 redecompose=lambda subs, hint: [_sub("A+B", {"x.gd"})])
    subs = [_sub("A", {"x.gd"}), _sub("B", {"x.gd"}), _sub("C", {"x.gd"})]
    groups = orch.run_health_check(subs)
    assert orch.decompose_calls == 2, "原始拆解 + 重拆一次（不多拆）"
    assert sum(len(g) for g in groups) == 1, "重拆仍为 1 组 → 降级成单 coder"
    assert ("parallelism_one", "A") in orch.warnings, "白拆要留可观测指标"


async def test_health_check_accepts_redecomposed_plan():
    """重拆后并行度上来了 → 用重拆结果（不降级）。"""
    orch = _orch({"coder": remote_spec("coder")},
                 redecompose=lambda subs, hint: [_sub("A", {"a.gd"}),
                                                 _sub("B", {"b.gd"})])
    groups = orch.run_health_check([_sub("A", {"x.gd"}), _sub("B", {"x.gd"})])
    assert len(groups) == 2


async def test_health_check_redecomposes_with_llm_and_degrades():
    """`run` 走的异步体检：带 hint 重拆一次，仍为 1 组 → 降级单 coder。"""
    planner = PlannerLLM([{"title": "A", "brief": "还是一起改 x.gd",
                           "spec": "coder", "write_targets": ["x.gd"]},
                          {"title": "B", "brief": "又撞 x.gd",
                           "spec": "coder", "write_targets": ["x.gd"]}])
    orch = _orch({"coder": remote_spec("coder")}, llm=planner)
    out = await orch.run(Session("s"), "加存档系统", subtasks=[
        _sub("A", {"x.gd"}), _sub("B", {"x.gd"}), _sub("C", {"x.gd"})])

    assert orch.decompose_calls == 2, "原始拆解 + 重拆一次（不多拆）"
    assert out.steps == 1, "重拆仍为 1 组 → 降级成单 coder"
    assert planner.payload.count("重拆要求") == 0, "hint 是运行时拼的，不在原 payload"


async def test_pure_dependency_chain_is_not_degraded():
    """纯依赖链（无冲突）不算白拆——它买的是**隔离**不是并行。"""
    orch = _orch({"coder": remote_spec("coder")})
    subs = [_sub("勘察", set()), _sub("实现", {"a.gd"}, depends=["勘察"]),
            _sub("验收", set(), depends=["实现"])]
    groups = orch.run_health_check(subs)
    assert len(groups) == 1 and len(groups[0]) == 3, "依赖链不该被降级合并"


# ---------- ⑨-e 受保护文件 ----------

async def test_protected_file_escalates_to_user():
    """受保护文件 → 上抛用户（确认门），未获批准则整组不派发。"""
    orch = _orch({"coder": remote_spec("coder")}, protected=["project.godot"])
    subs = [_sub("A", {"project.godot"}), _sub("B", {"res://project.godot"})]
    orch.resolve_groups(subs)
    assert orch.escalations, "受保护文件必须上抛，不能让工人自己决定"
    assert orch.escalations[0].kind == "protected"

    await orch._resolve_escalations()           # 没 approver = 拿不到批准
    assert orch.blocked == {"A", "B"}
    assert ("protected_blocked", "project.godot") in orch.warnings


async def test_protected_approved_by_approver():
    """approver 批准 → 放行（受保护不等于禁止，是"人说了算"）。"""
    orch = _orch({"coder": remote_spec("coder")}, protected=["project.godot"],
                 approver=lambda q, c: True)
    orch.resolve_groups([_sub("A", {"project.godot"}),
                         _sub("B", {"res://project.godot"})])
    await orch._resolve_escalations()
    assert orch.blocked == set()


def test_unattended_forces_abort(monkeypatch):
    """CI / 无人值守：确认门没人应答 = 永久挂起 → 强制 abort。"""
    monkeypatch.setenv("CI", "1")
    orch = _orch({"coder": remote_spec("coder")}, protected=["project.godot"])
    assert orch.on_protected == "abort"
    hits = orch.plan_conflicts([Conflict("A", "B", "project.godot", "protected")])
    assert hits[0].action == "abort"


def test_load_protected_from_yaml(tmp_path):
    """protected.yaml：名单 + on_protected 策略；读不到回落内置名单（不是不保护）。"""
    (tmp_path / "p.yaml").write_text(
        "protected:\n  - project.godot\n  - 'migrations/**'\n"
        "on_protected: serialize\n", encoding="utf-8")
    names, action = load_protected(tmp_path / "p.yaml")
    assert names == ["project.godot", "migrations/**"] and action == "serialize"

    fallback, default_action = load_protected(tmp_path / "nope.yaml")
    assert "project.godot" in fallback and default_action == "ask"


async def test_protected_glob_matches_nested_path():
    """glob 形态的受保护名单要能命中嵌套路径（migrations/**）。"""
    orch = _orch({"coder": remote_spec("coder")}, protected=["migrations/**"])
    subs = [_sub("A", {"migrations/001_init.sql"}),
            _sub("B", {"migrations/001_init.sql"})]
    assert orch.detect_conflicts(subs)[0].kind == "protected"


# ---------- ⑨-f 可回滚 ----------

async def test_checkpoint_task_id_in_delivery(tmp_path):
    """编排前开检查点槽，task_id 进交付说明（一句 /rewind 能退回去）。"""
    from agent_godot.tools.godot.checkpoints import TaskCheckpoints

    (tmp_path / "a.gd").write_text("extends Node\n", encoding="utf-8")
    checkpoints = TaskCheckpoints(tmp_path)
    orch = _orch({"coder": remote_spec("coder")}, project_root=tmp_path,
                 checkpoints=checkpoints)
    out = await orch.run(Session("s"), "任务", subtasks=[_sub("A", {"a.gd"})])
    assert out.task_id and out.task_id in out.report
    assert checkpoints.list()[-1].task_id == out.task_id
    assert checkpoints.list()[-1].snapshots == 1, "声明的写目标进快照"


# =========================================================================
# ⑩ §1.6 任务书自包含：CONSTRAINTS + 自报假设 + 约定比对
# =========================================================================

def _prompt_of(llm: RecordingLLM) -> str:
    return " ".join((m.content or "")
                    for req in llm.requests for m in req.messages)


async def test_constraints_injected_unconditionally():
    """CONSTRAINTS 无条件注入（空也给显式标记），且与 digest **分段**。"""
    reg = _registry()
    llm = RecordingLLM([[text_ev("完成"), done_ev(None, "stop")]])
    spec = SubagentSpec(name="coder", role_prompt="实现者", tools=reg, model="fake")

    await spawn(spec, "做个存档", {"llm": llm, "model": "fake"})
    first = _prompt_of(llm)
    assert "项目硬性约定" in first and "（本项目暂无登记约定）" in first
    assert first.index("项目硬性约定") < first.index("交付要求"), "CONSTRAINTS 在交付要求前"


async def test_constraints_and_digest_are_separate_sections():
    """CONSTRAINTS 与 digest 必须分成两段——混在一起会被当成"参考背景"而非硬要求。"""
    reg = _registry()
    llm = RecordingLLM([[text_ev("完成"), done_ev(None, "stop")]])
    spec = SubagentSpec(name="verifier", role_prompt="验收员",
                        tools=reg.filter(readonly=True), model="fake")

    await spawn(spec, "验收存档", {
        "llm": llm, "model": "fake", "digest": "最近在改 save/ 目录",
        "constraints": Constraints(text="- 用 ConfigFile，禁止 JSON",
                                   rules=[Rule("存档用 ConfigFile", ("JSON",))])})
    prompt = _prompt_of(llm)
    assert "项目现状摘要（参考背景）" in prompt
    assert "项目硬性约定（不可协商，违反即验收不通过）" in prompt
    assert prompt.index("项目现状摘要") < prompt.index("项目硬性约定")
    assert "禁止 JSON" in prompt, "verifier 也要拿同一份约定（切断同源一起漏）"


def test_assumptions_extracted_from_report():
    """交付报告第 5 条 → 结构化假设清单（不靠聚合层解析自由文本）。"""
    report = ("1. 结论：完成\n2. 产出清单：save/save_manager.gd\n"
              "3. 关键决策：无\n4. 遗留：无\n"
              "5. 我的假设：\n"
              "- 任务书未指定序列化格式，我按社区常见做法选用了 JSON\n"
              "- 运行时路径我用了 res://\n")
    got = _extract_assumptions(report)
    assert len(got) == 2 and any("JSON" in a for a in got)
    assert _extract_assumptions("5. 我的假设：无\n") == []
    assert _extract_assumptions("没有第 5 条就不会瞎抽") == []


async def test_spawn_fills_assumptions_structurally():
    """spawn 把报告里的第 5 条回填成 SubtaskResult.assumptions 字段。"""
    reg = _registry()
    report = ("1. 结论：完成\n5. 我的假设：\n- 序列化格式我选了 JSON\n")
    llm = FakeLLM([[text_ev(report), done_ev(None, "stop")]])
    spec = SubagentSpec(name="coder", role_prompt="实现者", tools=reg, model="fake")

    out = await spawn(spec, "做个存档", {"llm": llm, "model": "fake"})
    assert out.assumptions and any("JSON" in a for a in out.assumptions)


async def test_constraint_violation_detected():
    """assumptions × CONSTRAINTS 求交：命中禁止项 → 进 conflicts。"""
    orch = _orch({"coder": remote_spec("coder")})
    results = [SubtaskResult(spec_name="coder", ok=True, title="实现",
                             report="完成", assumptions=["序列化我选了 JSON"])]
    rules = [Rule("存档用 ConfigFile", ("JSON",))]

    hits = orch.check_constraints(results, rules)
    assert hits and "JSON" in hits[0] and "ConfigFile" in hits[0]

    clean = [SubtaskResult(spec_name="coder", ok=True, title="实现",
                           report="完成", assumptions=["序列化选了 ConfigFile"])]
    assert orch.check_constraints(clean, rules) == []


async def test_no_rules_means_no_comparison():
    """冷启动（无登记约定）：只自报假设、不做比对（不假装查过了）。"""
    orch = _orch({"coder": remote_spec("coder")})
    results = [SubtaskResult(spec_name="coder", ok=True, title="实现",
                             report="完成", assumptions=["选了 JSON"])]
    assert orch.check_constraints(results, []) == []
    out = await orch.aggregate(results, task="加存档")
    assert "未经约定校验" in out.report, "必须显式提示这批产出没过约定检查"


async def test_constraint_violation_enters_aggregate_conflicts():
    """约定违反要进聚合的 conflicts（交 verifier 仲裁 / 用户看见），不能静默。"""
    orch = _orch({"coder": remote_spec("coder")},
                 constraints=Constraints(
                     text="- 用 ConfigFile，禁止 JSON",
                     rules=[Rule("存档用 ConfigFile", ("JSON",))]),
                 auto_arbitrate=False)
    results = [SubtaskResult(spec_name="coder", ok=True, title="实现",
                             report="完成", assumptions=["选了 JSON"])]
    out = await orch.aggregate(results, task="加存档")
    assert any("约定违反" in c for c in out.conflicts)
    assert out.ok is False


def test_load_constraints_from_markdown(tmp_path):
    """constraints.md：frontmatter 规则表（机检）+ 正文（注入）。"""
    (tmp_path / ".agent_godot").mkdir()
    (tmp_path / ".agent_godot" / "constraints.md").write_text(
        "---\nupdated_at: 2026-08-30\nrules:\n  - 存档用 ConfigFile | JSON\n---\n"
        "- 存档序列化用 ConfigFile，禁止 JSON\n- 命名 snake_case\n",
        encoding="utf-8")
    loaded = load_constraints(tmp_path)
    assert "snake_case" in loaded.text
    assert loaded.updated_at == "2026-08-30"
    assert loaded.rules and loaded.rules[0].forbids("我用了 JSON")
    assert not loaded.rules[0].forbids("我用了 ConfigFile")


def test_constraints_derive_bans_from_explicit_wording():
    """没有 frontmatter 规则表时，只从**显式禁令词**（禁止/不许…）派生。"""
    loaded = Constraints.from_markdown(
        "- 存档序列化用 ConfigFile，禁止 JSON\n- 命名 snake_case\n")
    assert any(r.forbids("选了 JSON") for r in loaded.rules)
    assert not any(r.forbids("snake_case") for r in loaded.rules), "不猜语义"


def test_constraints_are_capped():
    """约定膨胀要被截断（§1.6 ⑤-3：几百行塞进每个子任务会淹没重点）。"""
    body = "\n".join(f"- 约定{i}" for i in range(40))
    loaded = Constraints.from_markdown(body)
    assert loaded.truncated and "已截断" in loaded.text


async def _collect(bus: EventBus, sink: list) -> None:
    async for ev in bus.stream():
        sink.append(ev)
