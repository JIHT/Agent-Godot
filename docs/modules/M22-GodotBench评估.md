# M22 GodotBench 评估（基准集 · 组件级评估 · LLM-as-a-Judge · 四级能力曲线）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 17 · 里程碑 **MI-8「评估收官报告」——项目毕业礼** |
| 代码落点 | `benchmarks/`（tasks/runner/judges/reports，见 §0.5） |
| 前置模块 | 全部（被评对象是整个系统）；尤其 M17/M18（微调模型的裁决者） |
| 手写比例 | 100% 手写 |
| 教程映射 | 📝笔记 Agent 评估 · 📘 zero2Agent eval 篇 · SWE-bench 论文 |

---

## 0. 本模块在项目中的位置

**大白话**：**没有评估的 Agent 项目是玄学项目**——"感觉微调后变好了"不可信，"demo 能跑"≠"任务能做"。GodotBench 给整个项目装**高考体系**：一套标准化试卷（50 个可判定的 Godot 开发题）、机器阅卷（三级判定器）、模考曲线（base→SFT→LoRA→GRPO 四级对比）——**这份报告就是 22 个模块项目的成绩单**，也是"感觉"变成"数字"的唯一途径。三级评估各评一层：

```text
系统级：Godot 任务成功率 —— 评整个产品（网关+Loop+工具+校验全链路）
模型级：同任务纯模型直出 —— 评模型本身（四级曲线）
组件级：路由准确率/检索召回/压缩保真 —— 评每个子系统（M12/M10/M07 各自体检）
```

**交付后状态**：50 任务基准+自动跑分器+双裁判+四级能力曲线报告——放进 README，项目毕业。

---

## 0.5 ★ 施工文件清单（开工前必看的一页表）

**本模块你一共要新建 12 个文件/组**：

| # | 新建文件（完整路径） | 职责一句话 | 关键类/函数 | 预估行数 | 手敲步骤(§4) | 依赖 |
|---|---|---|---|---|---|---|
| 1 | `benchmarks/__init__.py` 等 | 空包 | — | 2 | 步骤 0 | — |
| 2 | `benchmarks/tasks/T001-T020.yaml+py` | 首批 20 题（easy 梯队） | `verify` 各 1 | 200 | 步骤 1 | M06 runner |
| 3 | `benchmarks/tasks/…T021-T050` | 补齐 50 题分布 | — | 300 | 步骤 3 | — |
| 4 | `benchmarks/tasks/snapshots/*.zip` | 每题初始项目 | — | 制作 | 步骤 1/3 | Godot |
| 5 | `benchmarks/task_def.py` | 任务装载与分割 | `BenchTask`、`load_tasks` | 50 | 步骤 1 | — |
| 6 | `benchmarks/runner.py` | 隔离执行+判定 | `Sandbox`、`BenchRunner` | 120 | 步骤 2 | 核心包 |
| 7 | `benchmarks/verdicts.py` | 判定协议 | `Verdict` | 30 | 步骤 1 | — |
| 8 | `benchmarks/judges/llm_judge.py` | 裁判+κ 校准+pairwise | `LLMJudge` | 100 | 步骤 4 | M02 |
| 9 | `benchmarks/judges/rubrics.py` | 评分细则（含反例） | 各维度 rubric | 60 | 步骤 4 | — |
| 10 | `benchmarks/component_eval.py` | 5 组件基准接入 | 各组件 harness | 100 | 步骤 6 | M10/M12/M07 |
| 11 | `benchmarks/isolation.py` | 防泄漏三闸 | `TaskIsolation` | 60 | 步骤 3 | — |
| 12 | `benchmarks/report.py`+`run.py` | 报告生成+CLI | `write_report/regression_gate` | 90 | 步骤 5/7 | matplotlib |

**完成后你拥有**：`python -m benchmarks.run --models all --split val --repeats 3` 一键出报告。

---

## 1. 知识点详解（每节五段：定义 → 大白话 · 举例 · 演进 · 易错点）

### 1.1 基准集设计（任务集是评估的灵魂）

**① 严格定义**：每任务四要素缺一不可：`prompt`（自然语言指令）、`initial_proj`（初始项目快照 zip——环境可重置）、`verifier`（机器判定函数）、`meta`（难度 tier/类别/预期步数）。50 题分布：脚本 14/场景 12/调试 10/API 知识 8/多步重构 6。难度以"人类 Godot 开发者耗时"锚定（easy<5min/medium 5~20min/hard>20min），3 个人类基线校准。**train/val/test 三分割**（test 只在最终报告跑一次——防刷题）。

**② 大白话**：**出高考试卷**。题干（prompt）不能有歧义（两个考生读出两个意思=废题）；考场环境（initial_proj）每题独立且可重置（不能让上一题的草稿留在桌上）；**标准答案要机器可判**（verifier：选择题涂卡机判，不是"阅卷老师觉得对"）；难度要有人类锚定（状元卷不能拿来考初中生）。分布要覆盖考纲（五类别）——偏科的试卷量出来的"分数"没有效度。

**③ 举例**：任务定义（YAML+verifier）：

```yaml
id: T023; tier: medium; category: scene_edit
prompt: "给 player.tscn 添加 Area2D 子节点 Hitbox，挂新 hitbox.gd，
        body_entered 连到 player.gd 的 _on_hitbox_entered"
initial_proj: snapshots/T023_proj.zip
verifier: benchmarks.tasks.T023:verify
```

```python
async def verify(proj_root, runner) -> Verdict:
    sf = parse_tscn((proj_root / "player.tscn").read_text())
    hitbox = sf.find("Player/Hitbox")
    if not hitbox or hitbox.type != "Area2D":
        return Verdict.fail("Hitbox 缺失或类型错误")
    if not any(c.get("signal") == "body_entered" for c in sf.connections):
        return Verdict.fail("信号未连接")
    check = await runner.check()                    # 复用 M06 校验器
    return Verdict.ok() if check.ok else Verdict.fail(check.errors[:1])
```

**④ 演进**：人工 demo 验收（不可复现）→ 单元测试（评组件不评系统）→ **任务基准集**（SWE-bench 2023 确立：真实 repo+issue+测试判定）→ 动态生成基准（防污染）。锚点：**好基准=真实分布+可自动判定+防泄漏**。

**⑤ 易错点**：
- verifier 过严（要求实现与参考一致）惩罚合理异构解——**判行为规格不判实现路径**（"信号已连且校验过"，不要求顺序/风格）
- 任务间初始项目独立（一题一 zip）——共享骨架导致跨题"积累记忆"
- prompt 歧义是最大 bug 源：每题两人独立读题做一遍，一致才入库

### 1.2 三级判定器（规则优先，LLM 兜底）

**① 严格定义**：判定金字塔——**越下越客观越上越宽容**：

```text
L1 结构判定：解析 .tscn/.gd 断言结构事实——零成本零偏差，但等价异构写法会挂
L2 运行判定：headless 跑测试/场景断言输出（M06 三级校验复用）——判行为，秒~分钟
L3 LLM-Judge：主观维度打分（代码质量/风格遵循 1~5）——覆盖规则盲区，但有偏差需校准
组合：pass = L1 结构要点 AND L2 运行通过；L3 只做诊断报告不进通过判定
```

**② 大白话**：**阅卷的三道工序**。选择题（L1 结构：涂卡机判——节点在不在、信号连没连，机器秒判零争议）；解答题（L2 运行：对照评分细则验算——headless 跑起来对不对，允许考生用不同解法）；作文（L3 裁判：文风好不好——**但作文分只进评语不进及格线**：作文分的主观性会污染"及格/不及格"这个硬判定）。原则：**确定性系统用规则、概率系统才用裁判**——Godot 域可验证（M06 红利）的最后一环。

**③ 举例**：runner 的执行-判定隔离：

```python
async def run_task(self, task, engine_factory) -> TaskRecord:
    proj = await self.sandbox.fresh(task.initial_proj)    # 每次全新沙箱
    result = await engine_factory().run_task(proj, task.prompt, budget=self.cfg.budget)
    verdict = await task.verify(proj.root, self.godot)    # ★判定器与被评者隔离
    return TaskRecord(task=task.id, verdict=verdict, result=result,
                      trace_path=await self.save_trace(result))  # 轨迹存档
```

**④ 演进**：人工验收→单元断言→LLM-Judge 滥用期（什么分都让模型打）→**分层判定**（客观归规则主观才裁判）——SWE-bench 的 pass-to-pass/fail-to-pass 是此思想极致。

**⑤ 易错点**：
- 隔离：verifier 与被评 Agent 不共享项目目录（各一份）
- 超时区分"做不完"与"死循环"（stop_reason 不同，报告分开——治理方向不同）
- 轨迹必须存档：失败分析、M17 数据、M18 复盘三处复用——**评估器同时是数据工厂**

### 1.3 LLM-as-a-Judge 的偏差与校准

**① 严格定义**：强模型评输出的四种已知偏差与对策：**位置偏差**（倾向先出现的→A/B 互换跑两次取均值）、**长度偏差**（偏爱长回答→维度拆分+长度归一提示）、**自我偏好**（偏爱同家族→裁判与被评异源）、**分数坍缩**（全打 4/5→强制 pairwise 排序或锚点校准）。校准流程：20 条人工金答案→裁判与人一致率（Cohen's κ）<0.6 换提示/换裁判。

**② 大白话**：**裁判也是人（模型），也会偏心**。位置偏差=先入为主（先看到的答案 anchoring 了评判标准）；长度偏差=“写得长显得认真"的错觉；自我偏好=同门师兄弟互相抬分；分数坍缩=和稀泥（都给 4 分谁也不得罪）。校准=**用人工标注的金答案给裁判做岗前考试**——一致率不够就换人（换提示/换模型），上岗后定期抽查（裁判模型升级=全历史分数作废，像换考纲）。

**③ 举例**：pairwise+位置互换：

```python
async def compare(self, out_a, out_b, task) -> str:
    r1 = await self.judge(judge_prompt(task, a=out_a, b=out_b))   # A更好/B更好/平
    r2 = await self.judge(judge_prompt(task, a=out_b, b=out_a))   # ★互换
    if flip(r2) == r1: return r1       # 两个方向一致才采信
    return "tie"                       # 不一致=平局
```

**④ 演进**：人工评估（贵）→ 单裁判（G-Eval/MT-Bench 2023）→ 偏差研究潮 → 多裁判集成+人工校准环 → 基于规则的奖励模型（回归客观方向）。

**⑤ 易错点**：
- 裁判温度 0（可复现）+**固定版本**（升级=历史分数作废）
- κ 按维度算（结构维度准、风格维度飘是常态）——一刀切"校准通过"是假象
- 评分标准要给**反例**（"以下不算遵循约定：…"）——否则裁判标准漂移

### 1.4 组件级评估与四级能力曲线

**① 严格定义**：系统分数不涨时组件评估定位"卡在哪"：QueryEngine 100 条标注意图→路由准确率；RAG 200 条 QA→recall@5/MRR/忠实度（RAGAS）；上下文 50 轮长任务→压缩后保真率；记忆→跨会话偏好遵循率；网关→降级链路通过率（M21 已建）。**四级曲线**（最终报告主图）：base→SFT→SFT+LoRA→GRPO，同任务集/同判定器/同预算唯一变量=模型——受控实验。

**② 大白话**：**体检报告的分项与四次模考**。总分（系统成功率）没涨时，分项（组件基准）告诉你哪科拖后腿——数学差补数学（修 M12 路由），不是全科刷题。四级曲线=同一考生（任务集）的四次模考：唯一变量是"上了几个培训班"（base 裸考→SFT 岗前培训→LoRA 专项→GRPO 应试强化）——**受控变量才能归因**：如果四次模考之间连考场都换了（判定器/预算不同），提分到底归谁就说不清了。

**③ 举例**：四级曲线跑法（报告脚本即伪代码）：

```python
MODELS = ["qwen2.5-coder-7b-base", "local/godot-coder-sft",
          "local/godot-coder-lora", "local/godot-coder-rl"]
async def report():
    rows = []
    for m in MODELS:
        recs = await BenchRunner(engine=lambda: make_engine(m),
                                 tasks=val_split, repeats=3).run_all()
        rows.append({"model": m, "pass_rate": mean(r.verdict.ok for r in recs),
                     "avg_steps": mean(r.steps), "usd_per_task": mean(r.usage.cost_usd)})
    chart = plot_curve(rows)              # 成功率主轴+成本/步数副轴
    write_report(rows, chart, await final_test_run())
```

**④ 演进**：端到端黑盒分→组件白盒定位→**回归门禁**（CI 跑组件基准，跌>2% 阻断合并——评估从"报告"变"护栏"）。

**⑤ 易错点**：
- 每模型 **3 seed 重复取均值±方差**（单次 pass_rate 噪声 ±5%）
- 成本与成功率一起报告（提分但步数翻倍=负优化）——**能力/成本双轴**
- test 集只在最终报告跑——每多跑一次无污染保证弱一分

---

## 2. 接口设计（完整签名）

```python
@dataclass
class BenchTask:
    id: str; tier: Literal["easy","medium","hard"]; category: str
    prompt: str; initial_proj: Path; verifier: Callable; expected: dict
def load_tasks(split: Literal["train","val","test"] | None = None) -> list[BenchTask]: ...

@dataclass
class Verdict: ok: bool; reason: str; details: dict | None

@dataclass
class RunConfig: budget: BudgetConfig; parallel: int = 4; repeats: int = 3
class Sandbox:
    async def fresh(self, snapshot_zip: Path) -> ProjectHandle: ...
class BenchRunner:
    async def run_all(self) -> list[TaskRecord]: ...
    def summarize(self, records) -> RunSummary: ...      # pass_rate/steps/cost ±方差

class LLMJudge:
    async def score(self, output, task, rubric) -> JudgeScore: ...
    async def compare(self, a, b, task) -> str: ...
    def calibrate(self, golden: list) -> KappaReport: ...

def write_report(rows, chart: Path, holdout) -> Path: ...
def regression_gate(component_scores, baseline, threshold=0.02) -> GateResult: ...
```

---

## 3. 关键难点参考片段：防"考卷泄漏"全链路

微调数据（M17/M18）与评估任务重叠→分数虚高。三道闸：

```python
class TaskIsolation:
    def ensure_no_overlap(self, train_prompts: list[str], split: str):
        # 1) 字面重叠：训练 prompt 与 test prompt 的 n-gram Jaccard > 0.6 → 拒入库
        # 2) 项目重叠：同初始项目（含微改）的任务不得横跨 train/test
        # 3) 语义抽查：embedding 近邻 top1 > 0.92 的 test 任务 → 人工复审
        ...
```

为什么难：泄漏形态越来越隐蔽（同题改数字、同项目换问法）——n-gram 抓不到语义级"换皮"，语义查重又有误伤（合理相似题）——**自动三闸+人工复审缺一不可**。

---

## 4. 手敲指引（函数级伪代码）

| 步骤 | 文件 | 函数级作用（伪代码） | 验证 |
|---|---|---|---|
| 1 | tasks 20 题+task_def | `手工制 20 道 easy（快照 zip 用 M06 样例项目改造）+verifier；load_tasks：yaml 装载+split 过滤` | 人类做 3 题全过（金标准校验） |
| 2 | runner+verdicts | `Sandbox.fresh：zip 解压到 tmp 唯一目录；run_task：§1.2 ③ 隔离执行+判定+轨迹存档` | 同题跑两次结果一致 |
| 3 | 加 30 题+isolation | `补齐五类分布；TaskIsolation 三闸（§3）跑全库` | 类别/难度矩阵覆盖、零泄漏 |
| 4 | judges | `LLMJudge：温度 0+固定版本；score：rubric（含反例）填提示；compare：§1.3 ③ 互换；calibrate：20 金答案算 κ 按维度` | κ>0.6 才上岗 |
| 5 | report+run | `summarize：均值±方差；write_report：四级曲线图（成本副轴）+失败归因表；run.py CLI` | base 模型基线出炉 |
| 6 | component_eval | `五组件 harness：意图准确率/recall@5/压缩保真/记忆遵循/降级通过——统一 GateResult` | 回归门禁进 CI |
| 7 | 四级曲线 | `全模型×3 seed 跑 val→报告；test 最终一次封存` | **毕业报告生成** |

---

## 5. 测试与验收

```python
async def test_verdict_is_deterministic(): ...
async def test_golden_human_solutions_pass(): ...   # 人工参考解全过（无误杀）
async def test_sandbox_isolation(): ...             # A 题写的文件不在 B 题沙箱
```

**验收 Demo（MI-8 毕业）**：`python -m benchmarks.run --models all --split val --repeats 3` → `reports/final-2026-08/`：四级能力曲线（成功率/成本双轴）、失败归因表、组件体检卡、test 集封存成绩。**这张图放进 README，项目毕业。**

---

## 6. 踩坑记录（留白自填）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

---

## 7. 面试拷打（附详细参考答案）

**1. 好基准的三条件？任务四要素哪个最容易做歪？**
答：三条件：真实分布（任务像用户真的会提的——不是玩具题）、可自动判定（无人参与可重复跑——否则每次评估一套成本）、防泄漏（评估与训练数据隔离）。四要素中最易做歪的是 **verifier**：两个方向的歪——过严（要求实现与参考解一致：节点创建顺序/代码结构都要一样→惩罚了合理异构解，分数反映"像不像我"而不是"能不能用"）；过松（只 check 语法过→模型写个空实现也过，分数虚高）。校准手段：**人类参考解全过+对抗样本（故意歪解）全挂**——两头的假阳/假阴率都压到零，verifier 才可信。

**2. "判行为规格而非实现路径"怎么把握分寸？**
答：规格=用户可观察的行为契约（"信号连接后 headless 运行无报错且回调被触发"）；路径=实现细节（用 connect() 代码连还是编辑器 scene 文件连、变量叫什么、先建脚本还是先建节点）。把握方法：**从任务 prompt 反推验收**——prompt 里承诺给用户的（"碰到会减血"）就是规格必测项；用户没承诺的都是自由度（Godot 实现同一信号连接至少三种写法，全部应通过）。过严的代价信号：多个不同的正确解只有一个能过（异构惩罚）；过松的代价信号：删掉核心功能的"偷工解"也能过——两边都要拿样本试。

**3. 三级判定器的分层逻辑？为什么通过判定不用 LLM-Judge？**
答：分层逻辑=按客观性递减排序使用：L1 结构（确定性 100%）→L2 运行（确定性高：跑起来对就是对）→L3 主观（有偏差）。通过判定不用 L3 的三个理由：①**可复现性**——基准的分数要能跨时间比较（今天 62% 下月 65% 说明改进了），LLM 裁判即使温度 0 也有版本漂移风险，规则判定百年不变；②**成本与速度**——50 题×4 模型×3 seed=600 次评估，L1 毫秒级零成本，LLM 裁判每次一次调用；③**无偏差争议**——通过/不通过是硬结论（进四级曲线对比），主观分数的偏差会系统性扭曲模型间对比（自我偏好恰好偏向某家族）。L3 的正确位置：诊断报告（失败原因归类——这里"大致对"就够用）。

**4. LLM-Judge 四偏差与对策？κ 校准怎么做？**
答：四偏差对策见 §1.3 表（位置→互换重跑；长度→维度拆分；自我→异源裁判；坍缩→pairwise）。κ 校准：①造金答案——20 条人工标注（覆盖好/中/差各分数段）；②裁判跑金答案，与人的分级一致性算 Cohen's κ（消除随机一致性的一致率度量：κ=1 完全一致，0.6+ 可用，0.8+ 优秀）；③κ<0.6 的处置顺序：先改提示（给锚点样例+反例）→再改输出格式（打分改排序）→最后换裁判模型；④**按维度分别算 κ**（结构维度可能 0.8、风格维度 0.5——一刀切"通过"掩盖了飘的维度）。上岗后：裁判版本锁死+季度抽查。

**5. pairwise 互换位置解决什么？不一致为什么判平而不是三局两胜？**
答：互换解决位置偏差（裁判倾向先出现的答案）：同一对 (A,B) 换序再问一次，两个方向都说 A 好→位置无关的真偏好（采信）；一边说 A 一边说 B→裁判的判断被顺序主导=对这对样本无可靠信号。不一致判平而非三局两胜的理由：①三局两胜**没有消除偏差只是稀释**（位置偏差系统性存在时，多做几次还是按位置投）；②"平局"是诚实的元信息——它标记了"A 与 B 在裁判分辨率之下无显著差异"，把这对样本计入统计（平局率高的区域=裁判分辨率极限，该换更细的 rubric 而不是硬分胜负）；③省成本：两次出结果，不用第三次。

**6. 多 seed 重复的意义？单次跑分的噪声有多大？**
答：采样随机性（temperature>0）下单次 pass_rate 是**伯努利过程的样本统计**：50 题、真实通过率 60% 时，单次的标准差=√(0.6×0.4/50)≈7%——**单次 52% 和 68% 可能是同一个模型**！四级曲线的相邻两级（如 SFT 60%→LoRA 66%）差 6 个点，完全淹没在 ±7% 噪声里——不重复测量的"提升"是玄学。做法：每配置 3 seed 取均值±标准差；报告里画误差条（error bar）；两模型比较用配对检验（同任务同 seed 的差异做符号检验——配对消除了任务难度方差，比独立比较灵敏得多）。这是"实验科学素养"在 LLM 评估的具体化。

**7. 能力/成本双轴报告的必要性？"提分但负优化"举例？**
答：单轴成功率的问题：隐藏了代价——模型可以靠"每任务多想 3 倍步数"换 5 个点的通过率。负优化实例：GRPO 后某模型 pass_rate 64%→68%，但平均步数 12→21、成本 $0.08→$0.19/任务——同样的预算下（用户套餐每任务限 $0.10）它反而不可用——**分数涨了产品劣化了**。双轴报告（成功率主轴+成本/步数副轴）强制呈现 tradeoff：帕累托前沿上的点才值得选（同等成本下最高分，或同分数下最低成本）。推论：回归门禁也应是双阈值的（分数跌>2% 或成本涨>15% 都阻断）。

**8. 评估器怎么同时是数据工厂？（轨迹三复用）**
答：BenchRunner 每次跑分都存档完整轨迹（事件流+usage+verdict）——同一份数据三处复用：①**失败分析**——失败任务的轨迹聚类归因（哪类任务挂、挂在哪步：检索没找到/工具用错/校验不过），指导下一轮迭代方向；②**M17 训练数据**——成功轨迹直接转 SFT 样本（trajectory_to_sample），失败轨迹改写成纠错对——评估集自动产出训练集（注意隔离闸：train split 的轨迹才可用）；③**M18 复盘与课程**——GRPO 的成功率统计（难度带筛选的原料）与人工抽检样本（KL 漂移检测）。设计含义：**轨迹格式（M03 事件流）是评估与训练的共同语言**——当初设计事件溯源（M09）时的"顺便"，在此兑现为主要收益。

**9. 考卷泄漏的三道闸各抓什么形态？**
答：①n-gram Jaccard 抓**字面复制**——训练语料里直接出现 test 题（含改数字的近复制）；②项目 hash 分组抓**同源换皮**——同一初始项目的不同问法（数据集构建时常见的"一题多问"扩充法，训练和评估各拿一半=泄漏）；③embedding 近邻抓**语义等价**——题目换了说法但本质同题（"给玩家加双跳"vs"让角色能跳两次"，字面零重叠）。人工复审兜底的原因：三闸都有盲区（n-gram 不懂语义、语义查重误伤合理相似——"加敌人"和"加第二个敌人"相似度高但确实是不同任务）——自动闸做初筛收敛范围，人做最终裁决。泄漏的破坏力数据：SWE-bench 等基准被污染的实测显示虚高可达 10+ 个点——**评估的公信力就是项目的公信力**。

**10. 开放题：50 题被刷到 95%+，下一步？**
答：基准是需要**运营的资产**，饱和后的四条路：①**难度爬升**——现有题加维（多步重构 3 步→8 步、跨文件依赖、性能约束"60fps 内"）——便宜但只是把天花板推迟；②**动态生成**——参数化任务模板（骨架生成器：随机组合节点类型×信号×约束生成新题+配套 verifier）——理想形态但 verifier 的正确性要自动验证（生成器可能造出无解题——需人类抽样把关联）；③**对抗更新**——从失败轨迹里发现新任务类别（模型挂得集中的模式=新题源），基准跟随真实弱点生长；④**维度扩展**——95% 是"能做"的饱和，新维度没饱和：效率（同任务步数/成本排名）、健壮性（含干扰项目/歧义 prompt 下的表现）、多轮交互（需要澄清提问的任务）。战略提醒：基准的目的是**指导改进**不是贴金——刷满分的基准已失去指导价值，及时让它"难回去"才是运营。

---

## 8. 教程映射与延伸

- 必读：SWE-bench 论文（任务即 issue、pass-to-pass 判定范式）；MT-Bench/G-Eval（judge 方法论）
- 选读：RAGAS 文档；AgentBench；HumanEval 污染研究（防泄漏现实案例）
