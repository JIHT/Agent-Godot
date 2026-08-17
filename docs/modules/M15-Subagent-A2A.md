# M15 Subagent 与 A2A（编排 · 隔离 · 并行 · 任务委托）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 10 · 里程碑 MI-4「完整 Agent 形态」收官 |
| 代码落点 | `backend/agent_godot/agent/orchestrator.py` + `tools/builtin/task_tool.py` + `tools/tool_filter.py` |
| 前置模块 | M13（multi 模式状态机在此合体）· M07（子代理上下文独立预算）· M09（子代理权限收窄） |
| 手写比例 | 100% 手写 |
| 教程映射 | 📘 zero2Agent 09 课 · 📗 hello-agents tools/task_tool.py（对照）· A2A 协议文档 |

---

## 0. 本模块在项目中的位置

单 Agent 的上下文是稀缺资源（M07 全篇在省钱），复杂任务（"重构战斗系统并同步更新测试与文档"）会把一个上下文塞爆且互相干扰。Subagent = **派生轻量 Agent，独立上下文、收窄工具、单一目标，完成后只交回结论**：

```text
主 Agent（编排者）
 ├─ Subagent A：改战斗逻辑（工具限 scripts/**）     ┐
 ├─ Subagent B：更新测试（工具限 tests/**）          ├─ 并行，互不见对方上下文
 └─ Subagent C：更新文档（只读+写 docs/**）          ┘
```

**交付后状态**：multi 模式可用——`/multi 并行完成战斗重构+测试+文档`，三子代理同时开工、互不污染、失败隔离；支持把任务委托给"另一个进程/另一台机器的 Agent"（A2A 雏形）。

---

## 1. 知识点详解

### 1.1 为什么需要 Subagent：上下文隔离经济学

**① 原理**

主 Agent 派生子代理的三个收益：

```text
上下文隔离   子代理读的 50KB 代码/日志不进主上下文——只回传 1KB 结论。
             主上下文保持"指挥视角"（任务状态板+结论），不被执行细节淹没
工具收窄     子代理只见 3~5 个相关工具（M04 registry.filter），选择准确率↑
权限收窄     子代理的路径白名单更紧（只能动 scripts/**），爆炸半径↓
失败隔离     子代理崩了/超时/死循环，主 Agent 收到"失败+原因"，主线不中断
```

一句话：**子代理是"有损耗压缩的信息探针"**——把大量探索性交互压缩成结构化结论。这也是它和"多轮工具调用"的本质区别：工具调用共享主上下文，子代理不共享。

**② 演进**：单 Agent 全干（上下文膨胀）→ 手动分会话（人肉编排）→ Subagent 机制（AutoGPT 的任务派生雏形）→ Claude Code 的 Task 工具（子代理+专属提示模板）→ **A2A 协议**（2024 Google：Agent 之间互为对等服务器，任务级委托标准化——Subagent 的跨进程/跨厂商形态）→ 多智能体框架（CrewAI/AutoGen 的角色编排，本项目手写其核心思想）。

**③ 最小案例**：派生模型（数据结构先于代码）

```python
@dataclass
class SubagentSpec:
    role: str                                   # "代码修改者"/"测试工程师"
    goal: str                                   # 单一、可验收的目标
    tools: list[str]                            # 工具白名单（names）
    paths: list[str] | None                     # 路径白名单（M09 规则注入）
    context_brief: str                          # 主代理给的背景（不含全过程！）
    budget: BudgetConfig                        # 独立预算（默认主预算的 1/4）
    returns: str                                # 期望返回物格式说明

@dataclass
class SubagentResult:
    spec_id: str; ok: bool; conclusion: str     # 结论（进主上下文的全部）
    artifacts: list[str]                        # 产出物引用（文件路径/diff id）
    usage: Usage; stop_reason: str              # 成本回传主代理记账
```

**④ 易错点**
- `context_brief` 要**刻意精简**——把主上下文摘要全塞给子代理就失去了隔离意义（"传任务卡，不传日记"）
- 子代理也要过权限门与确认门（M09）：收窄路径后大多数操作免确认，但 deny 规则必须继承
- 子代理的数量上限与总预算熔断：3 个子代理并行烧钱 ×3，主预算必须能看到聚合消耗

### 1.2 Orchestrator：派生 · 并行 · 聚合

**① 原理**

编排器职责：拆解（或接收主 Agent 的拆解）→ 并行派发 → 收集聚合 → 冲突检测：

```python
class Orchestrator:
    async def run(self, session, spec_list: list[SubagentSpec]) -> OrchestratorReport:
        async with TaskGroup() as tg:                      # 并行派发（结构化并发）
            tasks = {s.spec_id: tg.create_task(self._run_subagent(session, s))
                     for s in spec_list}
        results = {sid: t.result() for sid, t in tasks.items()}
        conflicts = self.detect_conflicts(results)         # 文件级冲突检测
        return OrchestratorReport(results=results, conflicts=conflicts,
                                  total_usage=merge_usage(results.values()))
```

**冲突检测**：两个子代理都改了 `player.tscn`（各自的乐观锁基于同一初始版本）——后提交者在聚合阶段撞锁。策略：**文件级互斥分配**（拆解时 paths 白名单不重叠，从源头防）+ 聚合时锁冲突检测兜底（同文件多 diff → 主 Agent 仲裁合并或串行重放）。

**② 演进**：串行委派（简单但慢）→ 并行 TaskGroup（Python 3.11 结构化并发的正用：一个子代理崩，同组全取消，不泄漏）→ 分层编排（子代理再派孙代理，预算层层扣减——本项目限两层防爆）。

**③ 最小案例**：子代理的运行环境装配（全部复用已有件）

```python
async def _run_subagent(self, session, spec: SubagentSpec) -> SubagentResult:
    sub_session = Session.new(parent=session.id)           # 独立会话（事件单独落库）
    tools = self.registry.filter(names=spec.tools).namespaced("sub")  # 视图裁剪
    permissions = self.rules.narrow(paths=spec.paths)      # 规则收窄
    engine = AgentEngine(profile=mode_profile_for(spec.role),
                         tools=tools, rules=permissions)    # 复用 M13 装配
    result = await engine.run(sub_session, spec.context_brief + "\n目标：" + spec.goal)
    return SubagentResult(ok=result.stop_reason == "natural",
                          conclusion=result.final_text[:2000],   # 结论也限长
                          usage=result.usage_total, ...)
```

**④ 易错点**
- 子代理的确认门回调要**上浮到主会话**（子代理等确认时，用户看到的是主界面的一个确认请求——M09 的 PendingConfirm 带 parent 链）
- TaskGroup 一个失败全取消的默认行为要不要 override？本项目：子代理失败不取消兄弟（隔离语义），用 `asyncio.gather(return_exceptions=True)` 而非 TaskGroup（这里与 3.11 惯例相反，语义优先）
- 子代理事件流打上 `sub:{spec_id}` 前缀进总线——前端能看到"分支剧情"但默认折叠

### 1.3 task_tool：把"派生"做成工具（模型自决编排）

**① 原理**

编排有两种触发：**显式**（multi 模式下主 Agent 一次性拆 N 个 spec，M13 流程）与**隐式**（任意模式下，模型自己判断"这步适合派个子任务"，调用 `task` 工具）：

```python
@register_tool(readonly=False, risk="medium")
class TaskTool(BaseTool):
    """派生一个子代理去完成独立子任务。适合：大批量读操作/独立模块修改/探索性调查。
    不适合：需要当前上下文细节的操作（子代理看不到本对话）。"""
    class Params(BaseModel):
        role: str = Field(description="子代理角色，如 '测试工程师'")
        goal: str = Field(description="单一明确的目标+验收标准")
        tools: list[str] = Field(description="允许的工具名列表")
        paths: list[str] | None = None
```

工具 description 里的"适合/不适合"指引极其重要——模型派生子代理的最大误用是"把需要上下文的事派出去"（子代理缺背景，做出来驴唇不对马嘴）。隐式派生让普通 craft 任务也能享受隔离（"顺便查一下所有 TODO"派个只读子代理）。

**② 演进**：框架写死编排流（CrewAI 的 crew.process）→ 编排也是工具（Claude Code 的 Task：模型在 ReAct 循环里自主决定派生）→ A2A（编排对象从"进程内子代理"扩展到"网络上的其他 Agent 服务"）。趋势：**编排逻辑从代码搬进模型决策**，代码只保留安全护栏（预算/路径/层数）。

**③ 最小案例**：并行批量只读的典型收益

```text
任务："审查整个项目所有脚本的性能问题"
单 Agent：读 40 个文件 → 上下文 +120KB → 后半程遗忘前半程（Lost in middle）
task 工具：模型派 5 个子代理各审 8 个文件（并行，各 25KB 上下文）
         → 各回传 top3 问题 → 主上下文只 +5KB 结论
```

**④ 易错点**
- task 工具本身要计入深度（`current_depth + 1 <= MAX_DEPTH=2`），否则子代理派孙代理无限套娃
- 子代理结论的 2000 字符截断要有结构要求（结论模板：做了什么/改了哪些文件/未决问题），裸文本截断丢关键信息
- 隐式派生的 risk 至少 medium（起子代理=烧钱），默认走确认门（"本次会话总是允许"可免，M09 remember 机制）

### 1.4 A2A：跨进程任务委托（雏形实现）

**① 原理**

Subagent 是**同进程**的（共享代码/配置/文件系统），A2A（Agent-to-Agent）把它推到网络两端：你的 Agent 把任务委托给"另一个 Agent 服务"（同事的 Godot 优化专家 Agent、云端代码审查 Agent）。协议三要素：

```text
Agent Card   GET /.well-known/agent.json —— 能力自述（名称/技能/端点/鉴权）
Task 对象    {id, status: pending|running|input-required|completed|failed,
              artifacts[]} —— 异步任务生命周期（注意 input-required：远端也要确认门！）
消息与产物    消息（对话式）+ artifacts（文件级产物）分离
```

本项目实现**最小 A2A 客户端**（把远端 Agent 包装成本地 task 工具的一个 provider）与**最小服务端**（把本地 Subagent 暴露成 A2A 端点）——重点是理解协议与 Subagent 的映射：**A2A Task ≈ 跨进程的 SubagentSpec/Result**。

**② 演进**：MCP（工具级标准：函数调用）→ A2A（任务级标准：长时异步委托）→ ANP/Agentic Web（更大愿景）。三者层次：MCP 给模型"手"，A2A 给 Agent"同事"。面试区分：**MCP 是 model↔tool 协议，A2A 是 agent↔agent 协议**。

**③ 最小案例**：远端 provider 桥接

```python
class RemoteA2AProvider:
    async def discover(self, base_url: str) -> AgentCard: ...
        # card = await get(f"{base_url}/.well-known/agent.json")
        # 校验 skills 是否匹配需求 → 摘要进本地技能目录（M14 Skills 联动！）
    async def submit(self, base_url, task: SubagentSpec) -> str: ...
        # spec → A2A Task JSON，POST /tasks/send
    async def poll(self, task_id) -> SubagentResult: ...
        # status 轮询/SSE 订阅；input-required → 转成本地确认门事件（穿透！）
```

**④ 易错点**
- 远端的 `input-required` 状态必须桥接回本地确认门，否则任务永远挂起——**确认门是端到端的**
- 超时与重试：网络任务的预算要含轮询间隔与总时长上限（远端 Agent 也可能死循环）
- 安全：远端 Agent Card 自述的能力不可信（要不要把代码发给它？），路径级别的内容过滤要过 M09 deny 规则

---

## 2. 接口设计（完整签名）

```python
# agent/orchestrator.py
@dataclass
class SubagentSpec / SubagentResult / OrchestratorReport: ...     # 见 1.1/1.2

class Orchestrator:
    def __init__(self, engine_factory: Callable[[], AgentEngine],
                 rules: RuleEngine, registry: ToolRegistry,
                 bus: EventBus, max_parallel: int = 4,
                 max_depth: int = 2): ...
    async def run_parallel(self, session, specs: list[SubagentSpec]) -> OrchestratorReport: ...
    def detect_conflicts(self, results) -> list[FileConflict]: ...
    # FileConflict: path, specs: [spec_id], resolution: pending|merged|escalated

# tools/builtin/task_tool.py（隐式派生工具，1.3 已给签名）

# tools/tool_filter.py
def filter_for_role(registry: ToolRegistry, role: str) -> ToolRegistry: ...
    # 角色预设：reader(只读) / coder(读写) / reviewer(只读+diff) / runner(headless)

# a2a/（可选进阶，Sprint 10 时间内完成最小可用）
class RemoteA2AProvider: ...
class A2AServer:                     # 把本地 subagent 能力暴露为 A2A 端点
    def mount(self, app) -> None: ...
```

## 3. 关键难点参考片段：冲突仲裁

两个子代理都产出对 `player.tscn` 的修改（各自基于初始版本）——乐观锁在聚合时必然冲突一个：

```python
def detect_conflicts(self, results: dict[str, SubagentResult]) -> list[FileConflict]:
    by_file: dict[str, list[str]] = defaultdict(list)
    for spec_id, r in results.items():
        for path in r.artifacts_touched():
            by_file[path].append(spec_id)
    return [FileConflict(path=p, specs=s) for p, s in by_file.items() if len(s) > 1]

async def resolve(self, session, conflict: FileConflict, results) -> str:
    # 策略：不自动合并。把两份 diff + 各自目标交给主 Agent 仲裁：
    arbitration = await self.loop.run(session, f"""两个子任务都修改了 {conflict.path}：
A（{results[conflict.specs[0]].spec.goal}）的 diff：...
B（...）的 diff：...
请决定：采用 A / 采用 B / 手动融合三者之一，并说明理由。""")
    return arbitration.stop_reason
```

为什么难：自动三方合并（diff3）在语义级修改（场景结构调整）上错误率不可接受——**把仲裁交还主 Agent（模型语义合并）** 是当前工程共识，但仲裁本身的上下文构造（两份 diff+各自意图）要精心剪裁。

## 4. 手敲指引

| 步骤 | 文件 | 做什么 | 验证 |
|---|---|---|---|
| 1 | orchestrator.py | Spec/Result + 并行 run | 3 个 fake 子代理并行完成 |
| 2 | _run_subagent | 独立会话+工具/规则收窄 | 子上下文不进主 trace |
| 3 | 冲突检测 | detect + 仲裁流 | 双改同文件走仲裁 |
| 4 | task_tool.py | 隐式派生工具 | craft 任务中模型自主派只读调查 |
| 5 | 深度与预算护栏 | MAX_DEPTH + 聚合记账 | 套娃被拦、总账正确 |
| 6 | multi 模式合体 | M13 骨架接 orchestrator | /multi 三任务并行 Demo |
| 7 | a2a/（时间盒 1 天） | 最小 client | 远端 echo agent 跑通任务往返 |

## 5. 测试与验收

```python
async def test_subagent_context_isolation():
    # 子代理读取 50KB 文件后，主会话上下文 token 增量 < 3KB（仅结论）

async def test_one_failure_others_complete():
    # 3 子代理其中 1 个抛错：其余 2 个正常完成，报告含失败原因

async def test_depth_limit_blocks_grandchildren():
    # 二级子代理内调用 task 工具 → 工具返回 ok=False "超出深度限制"
```

**验收 Demo（MI-4 收官）**：`/multi "重构 player 的输入处理为状态机，同步补测试，并整理 API 文档"` → 三子代理并行（前端折叠展开可见各自剧情）→ 各自 headless 验证 → 聚合报告（改动清单/测试结果/文档 diff/总成本）→ 若人为制造两子代理同改一文件，观察仲裁流。

## 6. 踩坑记录（留白）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

## 7. 面试拷打

1. Subagent 相比"多轮工具调用"的本质区别？（上下文隔离经济学）
2. "传任务卡不传日记"——context_brief 怎么设计？
3. 工具收窄带来哪两个收益？（选择准确率 + 爆炸半径）
4. 并行子代理同改一文件怎么办？为什么不自动 diff3 合并？
5. TaskGroup 与 gather(return_exceptions=True) 在编排场景怎么选？为什么本项目选后者？
6. 隐式派生（task 工具）的最大误用模式？description 怎么防？
7. 子代理的确认门如何上浮主会话？A2A 的 input-required 如何穿透？
8. MCP 与 A2A 的层次差异一句话？
9. 子代理层数与预算的双重护栏为什么必须代码硬编码而非提示约定？
10. 开放题：设计子代理的评估指标（隔离收益比=主上下文节省/子代理总消耗？结论采纳率？），怎么测？

## 8. 教程映射与延伸

- 📘 zero2Agent 09 课（sub-agent）
- 📗 hello-agents `tools/task_tool.py`（单文件对照实现）
- 必读：A2A 协议官方文档（Agent Card / Task lifecycle 两节）
- 选读：Claude Code Subagents 文档；CrewAI/AutoGen 的编排模式对比文
