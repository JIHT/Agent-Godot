# M22 GodotBench 评估（基准集 · 组件级评估 · LLM-as-a-Judge · 四级能力曲线）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 17 · 里程碑 **MI-8「评估收官报告」——项目毕业礼** |
| 代码落点 | `benchmarks/`（tasks/runner/judges/reports） |
| 前置模块 | 全部（被评对象是整个系统）；尤其 M17/M18（微调模型的裁决者）、M13（组件级指标的来源） |
| 手写比例 | 100% 手写 |
| 教程映射 | 📝笔记 Agent 评估 · 📘 zero2Agent（eval 篇）· SWE-bench 论文（方法论源头） |

---

## 0. 本模块在项目中的位置

**没有评估的 Agent 项目是玄学项目**："感觉微调后变好了"不可信；"demo 能跑"不等于"任务能做"。GodotBench 给整个项目装上标尺：

```text
系统级：Godot 任务成功率 —— 评"整个产品"（网关+Loop+工具+校验全链路）
模型级：同任务集、纯模型直出 —— 评"模型本身"（基座 vs SFT vs LoRA vs GRPO 四级曲线）
组件级：路由准确率/检索召回/压缩后记忆保真 —— 评"每个子系统"（M12/M10/M07 各自的体检）
```

**交付后状态**：50 任务基准集 + 自动跑分器 + 双裁判（规则+LLM）+ 四级模型能力曲线报告——**这份报告就是整个 22 模块项目的成绩单**。

---

## 1. 知识点详解

### 1.1 基准集设计（任务集是评估的灵魂）

**① 原理**

每个任务 = 一个**可判定的 Godot 开发题**，四要素缺一不可：

```text
┌─ prompt        用户自然语言指令（"给玩家加三段跳中的第二段"）
├─ initial_proj  初始项目快照（zip，含预置的坑/结构）——环境可重置
├─ verifier      机器判定函数（三级：见 1.2）
└─ meta          难度 tier / 类别（场景/脚本/信号/资源/调试）/ 预期步数
```

任务分布（50 题设计目标）：

| 类别 | 数量 | 示例 | 考察 |
|---|---|---|---|
| 脚本编写 | 14 | "写一个会巡逻的敌人（导航点循环）" | GDScript 能力 |
| 场景编辑 | 12 | "给 player.tscn 加碰撞伤害的 Area2D 层级" | 结构化工具使用 |
| 调试修复 | 10 | "修好这个报错的信号连接"（预埋 bug） | 诊断回路（Reflection） |
| API 知识 | 8 | "4.3 里 max_contacts_reported 怎么用" | RAG/知识 |
| 多步重构 | 6 | "把输入处理重构为状态机并保持测试绿" | plan 模式/长任务 |

**难度标定**：以"人类 Godot 开发者耗时"锚定（easy <5min / medium 5~20min / hard >20min），并用 3 个真实人类基线跑一遍校准。**train/val/test 三分割**（M18 已用 train；test 只在最终报告跑一次——防"对着考卷刷题"）。

**② 演进**：人工 demo 验收（不可复现）→ 单元测试（评组件不评系统）→ **任务基准集**（SWE-bench 2023 确立范式：真实 repo+issue+测试判定；WebArena/AgentBench 同代）→ 动态生成基准（防污染，选读）。面试锚点：**好的基准 = 真实分布 + 可自动判定 + 防泄漏**。

**③ 最小案例**：任务定义（YAML + verifier 函数）

```yaml
# benchmarks/tasks/T023.yaml
id: T023
tier: medium
category: scene_edit
prompt: "给 player.tscn 添加一个 Area2D 子节点名为 Hitbox，
        挂上新建的 hitbox.gd，并把 body_entered 信号连到 player.gd 的 _on_hitbox_entered"
initial_proj: snapshots/T023_proj.zip        # 一个最小 platformer 骨架
verifier: benchmarks.tasks.T023:verify       # 可调用路径
expected: {nodes: 1, scripts: 1, connections: 1}
```

```python
# benchmarks/tasks/T023.py
async def verify(proj_root: Path, runner: GodotRunner) -> Verdict:
    sf = parse_tscn((proj_root / "player.tscn").read_text())
    hitbox = sf.find("Player/Hitbox")
    if not hitbox or hitbox.type != "Area2D":
        return Verdict.fail("Hitbox 节点缺失或类型错误")
    if not any(c.get("from_") == "Hitbox" and c.get("signal") == "body_entered"
               for c in sf.connections):
        return Verdict.fail("信号未连接")
    check = await runner.check()                     # 复用 M06 校验器
    return Verdict.ok() if check.ok else Verdict.fail(f"headless 未过: {check.errors[:1]}")
```

**④ 易错点**
- verifier 判"过严"（要求实现细节与参考一致）会惩罚合理异构解——判定**行为规格**而非实现路径（"信号已连且校验过"，不要求连接顺序/代码风格）
- 任务间初始项目互相独立（一个 zip 一个题），共享骨架会让模型在多题间"积累记忆"（跨题污染）
- prompt 歧义是最大 bug 源：每题至少让两个人独立读题做一遍，答案一致才入库（人类一致性校准）

### 1.2 三级判定器（规则优先，LLM 兜底）

**① 原理**

判定器金字塔——**越往下越客观，越往上越宽容**：

```text
L1 结构判定（最严）：解析 .tscn/.gd 断言结构事实（节点存在/信号连接/方法签名）
                    —— 零成本、零偏差、可复现；但换种等价写法就挂
L2 运行判定（次严）：headless 跑测试/运行场景断言输出（M06 三级校验全量复用）
                    —— 判"行为对不对"，允许实现自由；成本秒~分钟
L3 LLM-Judge（兜底）：对"主观维度"打分（代码质量/遵循用户风格约定 1~5 分）
                    —— 覆盖规则判不了的；但有偏差，必须校准（见 1.3）
组合策略：pass = L1 结构要点 AND L2 运行通过；L3 只做加分项报告不进通过判定
```

**确定性系统用规则、概率系统才用裁判**——本项目的通过判定全部走 L1+L2（Godot 域可验证红利的最后一环），LLM-Judge 仅用于诊断报告（"失败轨迹的失败原因归类"）。

**② 演进**：人工验收 → 单元测试断言 → LLM-as-a-Judge 滥用期（什么分都让模型打）→ **分层判定**（客观归规则、主观才裁判）——SWE-bench 用 pass-to-pass/fail-to-pass 测试即此思想的极致。

**③ 最小案例**：runner 的执行-判定隔离

```python
class BenchRunner:
    async def run_task(self, task: BenchTask, engine_factory) -> TaskRecord:
        proj = await self.sandbox.fresh(task.initial_proj)    # 每次全新沙箱
        engine = engine_factory()                             # 被评配置（模型/模式）
        result = await engine.run_task(proj, task.prompt,
                                       budget=self.cfg.budget)
        verdict = await task.verify(proj.root, self.godot)    # ★ 判定器与被评者隔离
        return TaskRecord(task=task.id, verdict=verdict, result=result,
                          steps=result.steps, usage=result.usage_total,
                          trace_path=await self.save_trace(result))  # 轨迹存档！
```

**④ 易错点**
- **隔离**：runner/verifier 与被评 Agent 不能共享任何状态（verifier 用的 Godot bin 可同，但项目目录必须两份）
- 超时即失败但要区分"做不完"与"死循环"（stop_reason 不同，报告分开统计——治理方向不同）
- 轨迹必须存档（trace_path）：失败分析、M17 轨迹数据、M18 复盘三处复用——**评估器同时是数据工厂**

### 1.3 LLM-as-a-Judge 的偏差与校准

**① 原理**

用强模型评弱模型输出的四种已知偏差与对策：

```text
位置偏差     倾向先出现的答案        → A/B 位置互换跑两次取均值
长度偏差     偏爱长回答              → 评分维度拆分+长度归一提示
自我偏好     偏爱同家族模型的输出    → 裁判与被评模型异源（GPT 评 Qwen 系）
分数坍缩     全打 4/5 分             → 强制排序（pairwise）替代打分，或锚点样例校准
校准流程     20 条人工标注"金答案" → 裁判与人的一致率（Cohen's κ）< 0.6 就换提示/换裁判
```

本项目裁判的三个实际用途：失败原因归类（bug 归因报告）/ 代码质量 1-5 分（诊断维度）/ 四级模型的成对比较（GRPO vs SFT 谁更好——pairwise 互换位置跑）。

**② 演进**：人工评估（贵）→ 单裁判 LLM（G-Eval/MT-Bench 2023 建立方法论）→ 偏差研究潮（位置/长度/自我偏好系列论文）→ 多裁判集成+人工校准环（当前最佳实践）→ 基于规则的奖励模型（RMS，方向上回归客观）。

**③ 最小案例**：pairwise + 位置互换

```python
async def compare(self, out_a: str, out_b: str, task: BenchTask) -> str:
    r1 = await self.judge(judge_prompt(task, a=out_a, b=out_b))   # "A更好/B更好/平"
    r2 = await self.judge(judge_prompt(task, a=out_b, b=out_a))   # ★ 互换
    if flip(r2) == r1: return r1                                   # 一致才采信
    return "tie"                                                   # 不一致=平局
```

**④ 易错点**
- 裁判模型温度设 0（可复现），并固定版本（裁判模型升级=全历史分数作废）
- κ 一致率要按维度算（结构维度裁判可能很准、风格维度很飘）——一刀切的"校准通过"是假象
- 判分提示里的评分标准要给**反例**（"以下不算遵循约定：……"），否则裁判标准漂移

### 1.4 组件级评估与四级能力曲线

**① 原理**

系统分数不涨时，组件评估定位"卡在哪"：

```text
组件基准（每模块收官时已在做，此处系统化）：
  QueryEngine   100 条标注意图 → 路由准确率（M12 的 30 条扩充）
  RAG           200 条 QA 对 → recall@5 / MRR / 忠实度（RAGAS）
  上下文工程    50 轮长任务 → 压缩后信息保真率（关键事实问答）
  记忆          跨会话偏好遵循率（M08 的验收自动化版）
  网关          降级链路混沌用例通过率（M21 已建）
模型级四级曲线（最终报告主图）：
  base → SFT → SFT+LoRA 域适配 → GRPO
  同任务集/同判定器/同预算，唯一变量=模型 —— 这就是受控实验
```

**② 演进**：只看端到端分（黑盒）→ 组件指标（白盒定位）→ **回归门禁**（CI 里跑组件基准，分数跌>2% 阻断合并——评估从"报告"变"护栏"）。

**③ 最小案例**：四级曲线的跑法（伪代码即报告脚本）

```python
MODELS = ["qwen2.5-coder-7b-base", "local/godot-coder-sft",
          "local/godot-coder-lora", "local/godot-coder-rl"]
async def report():
    rows = []
    for m in MODELS:
        runner = BenchRunner(engine=lambda: make_engine(m), tasks=val_split)
        recs = await runner.run_all(parallel=4)
        rows.append({"model": m, "pass_rate": mean(r.verdict.ok for r in recs),
                     "avg_steps": mean(r.steps for r in recs),
                     "usd_per_task": mean(r.usage.cost_usd for r in recs)})
    chart = plot_curve(rows)          # pass_rate 主曲线 + 成本/步数副轴
    test_holdout = await BenchRunner(...tasks=test_split).run_all()  # ★最终一次
    write_report(rows, chart, test_holdout)
```

**④ 易错点**
- 每模型跑分要**多 seed 重复 3 次取均值±方差**（采样随机性下单次 pass_rate 的噪声可达 ±5%）
- 成本与成功率一起报告（GRPO 提分但步数翻倍=负优化）——**能力/成本双轴**才是完整结论
- test 集只在最终报告跑：每多跑一次，test 的"无污染保证"就弱一分

---

## 2. 接口设计（完整签名）

```python
# benchmarks/tasks/
@dataclass
class BenchTask:
    id: str; tier: Literal["easy", "medium", "hard"]
    category: str; prompt: str
    initial_proj: Path; verifier: Callable
    expected: dict
def load_tasks(split: Literal["train", "val", "test"] | None = None) -> list[BenchTask]: ...

@dataclass
class Verdict:
    ok: bool; reason: str; details: dict | None = None
    @staticmethod
    def ok_(): ...
    @staticmethod
    def fail(reason: str): ...

# benchmarks/runner.py
@dataclass
class RunConfig:
    budget: BudgetConfig; parallel: int = 4; repeats: int = 3
    timeout_per_task: float = 600
class Sandbox:
    async def fresh(self, snapshot_zip: Path) -> ProjectHandle: ...   # 隔离副本
class BenchRunner:
    def __init__(self, engine_factory: Callable[[], AgentEngine],
                 tasks, config: RunConfig, godot: GodotRunner): ...
    async def run_all(self) -> list[TaskRecord]: ...
    def summarize(self, records) -> RunSummary: ...   # pass_rate/steps/cost ±方差

# benchmarks/judges/
class LLMJudge:
    async def score(self, output, task, rubric) -> JudgeScore: ...
    async def compare(self, a, b, task) -> str: ...                   # pairwise
    def calibrate(self, golden: list) -> KappaReport: ...

# reports/
def write_report(rows: list[RunSummary], chart: Path, holdout) -> Path: ...
def regression_gate(component_scores: dict, baseline: dict,
                    threshold: float = 0.02) -> GateResult: ...       # CI 门禁
```

## 3. 关键难点参考片段：防"考卷泄漏"的全链路

模型微调数据（M17/M18）与评估任务一旦重叠，分数即虚高。三道闸：

```python
class TaskIsolation:
    def __init__(self, all_tasks: list[BenchTask]):
        self.by_proj = {}                        # 初始项目内容 hash 分组
        for t in all_tasks:
            self.by_proj.setdefault(sha_zip(t.initial_proj), []).append(t.id)
    def ensure_no_overlap(self, train_prompts: list[str], split: str):
        # 1) 字面重叠：训练 prompt 与 test 任务 prompt 的 n-gram Jaccard > 0.6 → 拒绝入库
        # 2) 项目重叠：同初始项目（含微改）的任务不得横跨 train/test
        # 3) 语义抽查：embedding 近邻 top1 相似度 > 0.92 的 test 任务 → 人工复审
        ...
```

为什么难：泄漏的形态越来越隐蔽（同题改数字、同项目换问法）——n-gram 抓不到语义级的"换皮"，而语义查重又有误伤（合理相似题）——所以最后一步留人工，自动三闸 + 人工复审缺一不可。

## 4. 手敲指引

| 步骤 | 文件 | 做什么 | 验证 |
|---|---|---|---|
| 1 | tasks/ 20 题 | easy 梯队+verifier | 人类做 3 题全过（金标准） |
| 2 | Sandbox+runner | 隔离执行 | 同题跑两次结果一致 |
| 3 | 加 30 题 | 补齐分布 | 类别/难度矩阵覆盖 |
| 4 | judges | 裁判+κ 校准 | κ>0.6 才上岗 |
| 5 | summarize+报告 | 曲线+成本双轴 | base 模型基线出炉 |
| 6 | 组件基准 | 5 组件接入 | 回归门禁进 CI |
| 7 | 四级曲线 | 全模型跑分 | 最终报告生成 |

## 5. 测试与验收

```python
async def test_verdict_is_deterministic():
    # 同一成品项目跑 verify 两次，Verdict 完全一致（无随机性）

async def test_golden_human_solutions_pass():
    # 人工参考解全部 pass —— verifier 没有"误杀"（假阴性率 ~0）

async def test_sandbox_isolation():
    # 任务 A 的 Agent 写了文件，任务 B 的沙箱里不存在该文件
```

**验收 Demo（MI-8 毕业）**：`python -m benchmarks.run --models all --split val --repeats 3` → 终端进度条 + `reports/final-2026-08/` 生成：四级能力曲线图（成功率/成本双轴）、失败归因表、组件体检卡、test 集封存成绩。**这张图放进 README，项目毕业。**

## 6. 踩坑记录（留白）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

## 7. 面试拷打

1. 好基准的三条件？任务四要素哪个最容易做歪？
2. "判定行为规格而非实现路径"怎么把握分寸？（过严/过松各什么后果）
3. 三级判定器的分层逻辑？为什么通过判定不用 LLM-Judge？
4. LLM-Judge 四偏差与对策？κ 校准怎么做？
5. pairwise 互换位置解决什么？不一致为什么判平而不是三局两胜？
6. 多 seed 重复的意义？单次跑分的噪声有多大？
7. 能力/成本双轴报告的必要性？"提分但负优化"举例；
8. 评估器怎么同时是数据工厂？（轨迹三复用）
9. 考卷泄漏的三道闸各抓什么形态？
10. 开放题：你的基准 50 题，一个月后模型都刷到 95%+，下一步？（动态生成/对抗更新/难度爬升——基准是需要运营的资产）

## 8. 教程映射与延伸

- 必读：SWE-bench 论文（任务即 issue、pass-to-pass 判定范式）；MT-Bench/G-Eval（judge 方法论）
- 选读：RAGAS 文档；AgentBench；HumanEval 的污染研究（防泄漏的现实案例）
