"""tests/test_agent/test_paradigms.py —— M13 §5 验收：四模式引擎 + craft 验证回路 + plan DAG。

四个验收单测（§5）+ 两个补充（from_task 解析 / 审批拒绝干净退出）：
- 注册表 + 视图（ask 物理上不给写工具）
- plan 带环打回
- re-plan 保持已完成节点
- craft 写坏代码 → 验证错误回填 → 自修 → 通过

+ 五条分级校验单测（M06 §1.5 原子四级 ↔ M13 §7.7 累积档）：
- L1 只跑语法 / L1+ 加跑导入 / L3 累积跑到测试
- runner 缺某级方法 → 鸭子类型降级跳过（保证对极简 runner 向后兼容）
- verify=None / per_task → 不跑
- ModeConfig.verify 必须流入 VerifyLoop（防分级配置形同虚设）
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agent_godot.agent import AgentLoop, Dispatcher, Session
from agent_godot.agent.paradigms import (PARADIGMS, CraftStrategy, ModeConfig,
                                         PlanGraph, PlanNode, PlanStrategy,
                                         PlanCycleError, VerifyLoop)
from agent_godot.core import ToolCall
from agent_godot.tools import BaseTool, ToolMeta, ToolRegistry, ToolResponse
from agent_godot.tools.godot.headless import CheckResult

from .conftest import FakeLLM, done_ev, make_dispatcher, text_ev


# ---------- 假 LLM（实现 LLM Protocol 的 complete，供 plan 生成/re-plan） ----------

def _dag(*nodes) -> str:
    return json.dumps({"nodes": nodes}, ensure_ascii=False)


class PlanLLM:
    """返回预置 DAG JSON 的假规划器（只实现 complete）。"""

    def __init__(self, dag: list[dict]):
        self.dag = dag

    async def complete(self, req):
        return SimpleNamespace(content=json.dumps({"nodes": self.dag},
                                                   ensure_ascii=False))


async def _reject(plan_text: str) -> bool:
    return False


async def _accept(plan_text: str) -> bool:
    return True


# ---------- 注册表 + 视图 ----------

def test_mode_registration_and_views():
    """四模式全部注册；ask 的工具视图物理上不含写工具（工具集即能力边界）。"""
    assert set(PARADIGMS) == {"ask", "craft", "plan", "multi"}

    reg = ToolRegistry()
    reg.register(_attach(WriteTool, "read_file", readonly=True, risk="low"))
    reg.register(_attach(WriteTool, "write_file", readonly=False, risk="medium"))

    assert "write_file" not in PARADIGMS["ask"].tools_view(reg).names()
    assert "read_file" in PARADIGMS["ask"].tools_view(reg).names()
    # craft/plan 用全工具
    assert "write_file" in PARADIGMS["craft"].tools_view(reg).names()


# ---------- plan：带环打回 ----------

def test_plan_rejects_cyclic_dag():
    a = PlanNode(id="a", title="A", depends=["b"])
    b = PlanNode(id="b", title="B", depends=["a"])
    with pytest.raises(PlanCycleError):
        PlanGraph([a, b])


def test_plan_rejects_missing_dependency():
    with pytest.raises(PlanCycleError):
        PlanGraph([PlanNode(id="a", title="A", depends=["ghost"])])


async def test_plan_from_task_parses_dag():
    llm = PlanLLM([
        {"id": "1", "title": "打地基", "criterion": "project.godot 存在"},
        {"id": "2", "title": "盖楼", "depends": ["1"], "criterion": "main.tscn 存在"},
    ])
    graph = await PlanGraph.from_task(llm, "给游戏加存档")
    assert set(graph.nodes) == {"1", "2"}
    assert [n.id for n in graph.ready_nodes()] == ["1"]


# ---------- plan：re-plan 保持已完成节点 ----------

async def test_plan_replan_keeps_done_nodes():
    # 串行链 1 → 2 → 3：1 已完成，2 失败一次，re-plan 只重排 2/3，1 保持 done
    graph = PlanGraph([
        PlanNode(id="1", title="one"),
        PlanNode(id="2", title="two", depends=["1"]),
        PlanNode(id="3", title="three", depends=["2"]),
    ])
    graph.mark("1", "done")
    graph.mark("2", "failed")

    replan_llm = PlanLLM([
        {"id": "2", "title": "two-fixed", "depends": ["1"], "criterion": "ok"},
        {"id": "3", "title": "three", "depends": ["2"], "criterion": "ok"},
    ])
    await graph.replan_from(replan_llm, graph.nodes["2"])

    assert graph.nodes["1"].status == "done"          # 前驱不被重复执行
    assert graph.nodes["2"].status == "pending"       # 失败节点被重排
    assert graph.nodes["3"].status == "pending"       # 后代被重排
    assert graph.nodes["2"].title == "two-fixed"


# ---------- plan：审批拒绝干净退出 ----------

async def test_plan_approval_rejects_clean_exit():
    plan_llm = PlanLLM([{"id": "1", "title": "a", "criterion": "x"}])
    loop = AgentLoop(FakeLLM([]), make_dispatcher(), model="m")
    strategy = PlanStrategy(llm=plan_llm, loop=loop, approver=_reject)

    result = await strategy.run_plan_mode(Session("s1"), "任务")
    assert result.stop_reason == "error"
    assert "未获批准" in result.final_text


# ---------- craft：客观验证回路三段式 ----------

class WriteTool(BaseTool):
    """写工具（readonly=False，name=write_file，命中 VerifyLoop.WRITE_TOOLS）。"""
    meta = None

    class Params(BaseModel):
        content: str = ""

    def __init__(self, sink: list[str] | None = None):
        self.sink = sink if sink is not None else []

    async def run(self, content: str = "") -> ToolResponse:
        self.sink.append(content)
        return ToolResponse(ok=True, summary=f"wrote {content}")


def _attach(tool_cls, name: str, *, readonly: bool, risk: str):
    """实例化工具并设置实例级 meta（避免多个实例共享类级 meta 串名）。"""
    inst = tool_cls()
    inst.meta = ToolMeta(name=name, description=name,
                         readonly=readonly, risk=risk)
    return inst


class FakeRunner:
    """假 GodotRunner：按脚本返回校验结果（第一次失败、第二次通过……）。"""

    def __init__(self, results: list[CheckResult]):
        self._results = results
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    async def check(self, script: str | None = None) -> CheckResult:
        r = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return r


async def test_craft_auto_fixes_syntax_error():
    """写坏代码 → 收到验证错误 Observation → 改对；断言 fixes==1 且最终自然终止。"""
    reg = ToolRegistry()
    reg.register(_attach(WriteTool, "write_file", readonly=False, risk="medium"))

    runner = FakeRunner([
        CheckResult(False, [{"file": "x.gd", "line": 1, "msg": "Parse Error"}]),
        CheckResult(True, []),
    ])

    script = [
        [done_ev([ToolCall(id="c1", name="write_file",
                           arguments='{"content": "bad"}')], "tool_calls")],
        [done_ev([ToolCall(id="c2", name="write_file",
                           arguments='{"content": "good"}')], "tool_calls")],
        [text_ev("修好了"), done_ev(None, "stop")],
    ]

    strategy = CraftStrategy(runner=runner, max_fixes=3)
    loop = AgentLoop(FakeLLM(script), Dispatcher(reg), model="m",
                     verify_runner=runner)
    session = Session("s1")
    result = await loop.run(session, "写代码", mode="craft",
                            strategy=strategy)

    assert result.stop_reason == "natural"
    assert strategy.verify_loop.fixes == 1
    # 验证错误确实作为 Observation 回填进了会话（模型据此返工）
    feedback = [m.content for m in session.messages if m.role == "system"]
    assert any("校验未通过" in c for c in feedback)


# ---------- craft：分级校验（M06 §1.5 原子四级 ↔ M13 §7.7 累积档） ----------

class LevelRunner:
    """分级假 runner：methods 决定"实现"了哪些级别。

    未列入 methods 的级别**不定义对应方法**，用于验证 VerifyLoop 的鸭子类型
    降级——真实 GodotRunner 四级齐全，而极简 runner（如上面的 FakeRunner）
    只有 check()，两种都必须能跑。
    """

    def __init__(self, results: dict[str, CheckResult],
                 methods: tuple[str, ...] = ("L1",)):
        self._results = results
        self.calls: list[str] = []
        for lv in methods:
            if lv == "L2":
                self.import_assets = self._make(lv)
            elif lv == "L3":
                self.run_tests = self._make(lv)

    @property
    def available(self) -> bool:
        return True

    def _make(self, level: str):
        async def _run(*_a, **_k) -> CheckResult:
            self.calls.append(level)
            return self._results[level]
        return _run

    async def check(self, script: str | None = None,
                    timeout: float = 15.0) -> CheckResult:
        self.calls.append("L1")
        return self._results["L1"]


def _ok() -> CheckResult:
    return CheckResult(True, [])


def _err(file: str, line: int, msg: str) -> CheckResult:
    return CheckResult(False, [{"file": file, "line": line, "msg": msg}])


_ALL = ("L1", "L2", "L3")


async def test_verify_loop_l1_only_runs_syntax_check():
    """verify="L1" → 只跑 L1 语法（秒级），不碰 L2/L3。"""
    r = LevelRunner({"L1": _ok()}, methods=_ALL)
    vl = VerifyLoop(r, max_fixes=3, verify="L1")
    assert await vl.after_write("write_file", ToolResponse(ok=True), None) is None
    assert r.calls == ["L1"]


async def test_verify_loop_degrades_when_runner_lacks_level():
    """runner 未实现某级 → 跳过而非报错（对只有 check() 的 runner 向后兼容）。"""
    r = LevelRunner({"L1": _err("x.gd", 1, "Parse Error")}, methods=("L1",))
    vl = VerifyLoop(r, max_fixes=3, verify="L1+")     # 想要 L2，但 runner 没有
    out = await vl.after_write("write_file", ToolResponse(ok=True), None)
    assert r.calls == ["L1"], "L2 未实现应被跳过，不能抛 AttributeError"
    assert out is not None and "L1 语法校验" in out


async def test_verify_loop_l1plus_runs_import():
    """verify="L1+" → L1 通过后继续跑 L2 导入，拦资源断链。"""
    r = LevelRunner({"L1": _ok(), "L2": _err("a.tscn", 3, "资源不存在")},
                    methods=_ALL)
    vl = VerifyLoop(r, max_fixes=3, verify="L1+")
    out = await vl.after_write("write_file", ToolResponse(ok=True), None)
    assert r.calls == ["L1", "L2"]
    assert out is not None
    assert "L2 资源导入校验" in out and "资源不存在" in out
    assert vl.fixes == 1


async def test_verify_loop_l3_runs_tests_cumulatively():
    """verify="L3" → 累积跑到 L1+L2+L3；前两级过了才轮到 L3。"""
    r = LevelRunner({"L1": _ok(), "L2": _ok(), "L3": _err("t.gd", 9, "断言失败")},
                    methods=_ALL)
    vl = VerifyLoop(r, max_fixes=3, verify="L3")
    out = await vl.after_write("write_file", ToolResponse(ok=True), None)
    assert r.calls == ["L1", "L2", "L3"]
    assert out is not None
    assert "L3 测试校验" in out and "断言失败" in out


async def test_verify_loop_skips_when_no_level_configured():
    """verify=None / "per_task" → 完全不跑（per_task 由节点边界触发，M15）。"""
    for level in (None, "per_task"):
        r = LevelRunner({"L1": _ok()}, methods=_ALL)
        vl = VerifyLoop(r, max_fixes=3, verify=level)
        out = await vl.after_write("write_file", ToolResponse(ok=True), None)
        assert out is None, f"verify={level!r} 不应返回验证错误"
        assert r.calls == [], f"verify={level!r} 不应触发任何校验"


def test_craft_strategy_passes_verify_level_to_loop():
    """ModeConfig.verify 必须流入 VerifyLoop——否则分级配置形同虚设。"""
    assert CraftStrategy(runner=None).verify_loop.verify == "L1+"   # 类默认
    craft = CraftStrategy(runner=None, config=ModeConfig(verify="L3"))
    assert craft.verify_loop.verify == "L3"
