# M14 Hooks · Command · Skills（可插拔扩展三件套）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 10 · 里程碑 MI-4「完整 Agent 形态」 |
| 代码落点 | `backend/agent_godot/hooks/` + `command/` + `skills/`（10 个文件，见 §0.5） |
| 前置模块 | M09（权限 gate 就是第一个 pre-tool hook 的正式化）· M03（Hook 管线挂 Dispatcher） |
| 手写比例 | 100% 手写 |
| 教程映射 | 📘 zero2Agent 08 课 · 📝笔记 Hooks/Skills · Claude Code 扩展机制文档 |

---

## 0. 本模块在项目中的位置

**大白话**：核心功能全齐了，本模块回答**可持续性问题**：如何让别人（和三个月后的你）不改核心代码就加能力？答案是给房子预留**三个标准接口**：**Hook=水电点位**（装修时预留的插座接口——管线固定，谁都能往后插电器：审计、格式化、脱敏全是"电器"）；**Command=墙上的开关**（确定性直达——按灯开关不需要"跟房子商量"，`/rewind` 不经过 AI）；**Skill=书架上的手册**（《装修指南》平时不占地方（目录一行），用时取下来读全文（按需注入））。三件套=**横切、入口、知识**三条正交扩展轴。

**交付后状态**：写一个"GDScript 格式化" hook（写过的 .gd 自动格式化）；新增 `/checkpoint save` 零核心改动；`skills/打包发布.md` 让 Agent 突然"会"导出模板。

---

## 0.5 ★ 施工文件清单（开工前必看的一页表）

**本模块你一共要新建 11 个文件**：

| # | 新建文件（完整路径） | 职责一句话 | 关键类/函数 | 预估行数 | 手敲步骤(§4) | 依赖 |
|---|---|---|---|---|---|---|
| 1 | `hooks/__init__.py` 等 | 空包 | — | 2 | 步骤 0 | — |
| 2 | `hooks/pipeline.py` | 挂载点+优先级+veto/modify | `HookSpec/Result`、`HookPipeline` | 90 | 步骤 1 | 无 |
| 3 | `hooks/pre_tool/permission_hook.py` | M09 gate 的 hook 化 | `PermissionHook` | 30 | 步骤 2 | M09 |
| 4 | `hooks/post_tool/format_hook.py` | .gd 自动格式化 | `FormatHook` | 40 | 步骤 3 | pipeline |
| 5 | `hooks/post_tool/redact_hook.py` | 输出脱敏 | `RedactHook` | 30 | 步骤 3 | pipeline |
| 6 | `command/__init__.py` 等 | 空包 | — | 2 | 步骤 0 | — |
| 7 | `command/parser.py` | 斜杠解析+近邻提示 | `CommandParser` | 30 | 步骤 4 | 无 |
| 8 | `command/handlers/builtin.py` | 内置命令组 | `cmd_compact/rewind/skills/plan` | 80 | 步骤 4 | M07/M09 |
| 9 | `skills/__init__.py` 等 | 空包 | — | 2 | 步骤 0 | — |
| 10 | `skills/loader.py` | SKILL.md 扫描/解析/注入 | `Skill`、`SkillLoader` | 70 | 步骤 5 | 无 |
| 11 | `skills/builtin/打包发布/SKILL.md` 等 ×3 | 内置技能文档 | — | 60 | 步骤 6 | — |

**完成后你拥有**：三个扩展口全部可插拔；外部贡献者写 hook/command/skill 均不改 core。

---

## 1. 知识点详解（每节五段：定义 → 大白话 → 举例 → 演进 → 易错点）

### 1.1 Hook 管线：优先级排序的同步/异步执行

**① 严格定义**：Hook=挂在固定挂载点的回调链。六个挂载点：`pre_tool`（可否决/改参数——权限门、审计）、`post_tool`（可改写结果——格式化、脱敏）、`pre_loop`（可注入消息——预算告警）、`post_loop`（死循环上报）、`session_start`（记忆召回注入）、`session_end`（记忆抽取触发+异步任务 join）。三动作协议：**pass/modify/veto**——modify 链式传递（后见者看到先见者改过的参数）、veto 短路（后续全跳）。

**② 大白话**：**Web 框架的 middleware 搬进 Agent**——pre_tool 链=FastAPI 的 request middleware（请求进来挨个过：鉴权→审计→改写），post_tool 链=response middleware（响应出去挨个过：脱敏→格式化）。veto=middleware 里直接抛 403 短路；异步 hook（统计上报）=middleware 里的后台任务（记一笔就走，不等落盘）。你已经懂 FastAPI middleware，就懂了 80% 的 Hook。

**③ 举例**：管线执行核心（veto 短路+modify 链+异步）：

```python
async def run(self, point: str, ctx: HookContext) -> HookContext:
    for spec in self._hooks[point]:                  # 注册时已按 priority 排序
        if spec.async_:
            self._bg.create_task(spec.handler(ctx))  # 后台不等待
            continue
        result = await spec.handler(ctx)
        if result is None: continue                  # None=pass
        if result.action == "veto":
            raise HookVeto(spec.name, result.reason) # 短路（Dispatcher 捕获转 ToolResponse）
        if result.action == "modify":
            ctx = ctx.evolve(args=result.modified_args)  # ★链式：后续看到新参数
    return ctx
```

**④ 演进**：函数内直接写横切逻辑（散落各处，改一处忘三处）→ 装饰器（挂载点受限）→ 显式管线（Express/Koa/FastAPI middleware 直系亲戚）。**横切关注分离**是 50 年软件工程的主旋律（AOP→middleware→hooks）。

**⑤ 易错点**：
- modify 链"后见覆盖先见"：priority 即参数演化顺序，必须文档写明+测试钉死
- veto 异常要被 Dispatcher 捕获转 `ok=False` ToolResponse（§3），直接上抛炸 Loop
- 异步 hook 不能用已释放资源——session_end 时 join 全部后台任务兜底
- 同步 pre_tool 必须 <10ms（每个工具调用都过，慢 hook 放大全局延迟）

### 1.2 Command：不经过模型的确定性入口

**① 严格定义**：`/plan /compact /rewind 3 /skills list`——斜杠命令**绕过 LLM 直达功能**。三种产出：**direct_result**（直接显示：清单类）、**prompt_inject**（转模型输入：路由类）、**state_change**（改会话状态：控制类）。解析只用正则+空格分词（命令保持"人类可打"的简单性，复杂参数交给自然语言）。

**② 大白话**：**墙上的开关 vs 语音助手**。"开灯"你可以说"小爱同学开灯"（经过 AI，可能听岔），也可以直接按开关（确定性 100%、延迟 0ms、零成本）。Agent 里同理：`/rewind` 这种**高危且精确**的操作绝不能走"跟模型说 rewind"——模型可能理解错、可能反问、可能今天心情好给你 /plan 了。**当模型不确定时，命令是用户手里唯一的确定性把手**（Claude Code 的 /compact /rewind /model 同思想）。

**③ 举例**：

```python
@register_command("compact")
async def cmd_compact(cmd: Command, session: Session) -> CommandResult:
    await session.compact_now()                        # 直调 M07 Compressor
    return CommandResult.direct(f"已压缩，当前 {session.context_tokens} tokens")
```

**④ 演进**：CLI flags（git 风格）→ IM 斜杠命令（Slack/Discord）→ Agent 时代"确定性逃生舱"。趋势：命令与自然语言并存——**精确操作走命令，模糊意图走对话**，入口双轨。

**⑤ 易错点**：
- 命令名与工具名 namespace 区分（命令=用户入口，工具=模型入口，同名会疯）
- 路由型命令 args 为空要交互追问（"要规划什么任务？"）而非报错
- 控制型命令也过审计（/rewind 高危，记录操作者与目标——M09 审计三问）

### 1.3 Skills：知识外化的按需加载

**① 严格定义**：领域知识写成独立 SKILL.md（frontmatter：name/triggers/tools_needed/version + 正文方法论），**用到才全文注入**。机制三步：①扫描建目录（常驻上下文仅 ~200 token）②用户输入命中触发词或模型主动调 `skill_use` 工具③全文注入本轮（M07 Skills 分区记账）。

**② 大白话**：**书架上的专业手册**。把《Godot 打包发布指南》全文塞进每次对话=让工人背着整个书柜上班（窗口爆炸+注意力稀释——Lost in the Middle）。Skills 机制：书架上只放**目录卡**（一技能一行），干活遇到"要打包了"才取下那本手册翻开（注入）。对比记忆（M08）：**记忆存"发生过的事实"**（会过时可遗忘），**Skills 存"怎么做某类事的方法论"**（稳定、版本化管理）——员工的个人经历 vs 公司的标准作业程序 SOP。

**③ 举例**：SKILL.md 与加载：

```markdown
---
name: 打包发布
triggers: [打包, 导出, 发布, export, build]
tools_needed: [godot_run_headless]
version: 2
---
# Godot 打包发布检查清单
1. export_presets.cfg 存在且 preset 名与目标平台匹配
2. 图标与版本号检查（project.godot 的 config/version）
3. headless 导出：godot --headless --export-release "Windows Desktop" ...
4. 常见坑：模板未安装 / C# 项目要先 build / 资源 remap
```

```python
class SkillLoader:
    def catalog_prompt(self) -> str:      # 常驻目录：每技能一行
        return "\n".join(f"- {s.name}: 触发词 {s.triggers[:3]}" for s in self.skills)
    async def load(self, name: str) -> str:   # 按需全文
        return f"<skill name='{name}'>\n{self.by_name[name].body}\n</skill>"
```

**④ 演进**：用户手写长提示（每次粘贴）→ 提示词模板库 → Cursor Rules / Claude Skills（触发式注入）→ Agent 自主技能检索（目录做成工具，模型自己查自己载——本项目双轨：触发词提示+模型主动调）。

**⑤ 易错点**：
- 触发词误命中（"发布"误触发）→ 注入浪费；缓解：触发词只是**提示**，最终由模型判断要不要 skill_use
- 技能文档冲突（两个技能都写"标准流程"）→ 版本化+技能内互链
- 技能加载进 trace 并计 token（它是上下文成本，M07 分区记账）

### 1.4 三件套协同（一次 `/plan 打包并发布 Windows 版`）

命令 `/plan` 确定入口（不经过模型）→ plan 模式（M13）生成 DAG → "执行打包"节点运行时 pre_tool hook 或模型主动 skill_use 注入打包技能 → headless 导出执行 → post_tool hook 校验产物存在并记录。**命令定入口、Hook 做横切、Skills 供知识**——三条扩展轴正交，互不感知。

---

## 2. 接口设计（完整签名）

```python
# hooks/pipeline.py
@dataclass
class HookSpec:
    name: str
    point: Literal["pre_tool","post_tool","pre_loop","post_loop",
                   "session_start","session_end"]
    priority: int = 100                  # 小者先执行
    async_: bool = False                 # True=后台不阻塞
    handler: Callable[[HookContext], Awaitable["HookResult | None"]]

@dataclass
class HookResult:
    action: Literal["pass","modify","veto"] = "pass"
    modified_args: dict | None = None
    reason: str = ""                     # veto 理由→审计

class HookPipeline:
    def register(self, spec: HookSpec) -> None: ...
    async def run(self, point: str, ctx: HookContext) -> HookContext: ...
    async def join_background(self) -> None: ...    # session_end 兜底

# command/
@dataclass
class Command: name: str; args: str
@dataclass
class CommandResult:
    kind: Literal["direct","prompt_inject","state_change"]
    text: str | None; new_mode: str | None
class CommandParser: 
    def parse(self, input: str) -> Command | None: ...
class CommandRegistry:
    def register(self, name: str, handler) -> None: ...
    async def dispatch(self, cmd: Command, session: Session) -> CommandResult: ...
    def suggestions(self, name: str) -> list[str]: ...   # 近邻："是要 /plan 吗？"

# skills/
@dataclass
class Skill:
    name: str; triggers: list[str]; tools_needed: list[str]
    version: int; body: str; source: Path
class SkillLoader:
    def scan(self, roots: list[Path]) -> list[Skill]: ...
    def catalog_prompt(self) -> str: ...
    async def load(self, name: str) -> str: ...
```

---

## 3. 关键难点参考片段：HookVeto → ToolResponse 的翻译

veto 在管线里是异常，到模型那里必须是协议化 Observation——翻译层在 Dispatcher：

```python
async def _run_one(self, call: ToolCall) -> ToolResponse:
    try:
        ctx = await self.hooks.run("pre_tool",
                HookContext(tool=call.name, args=json.loads(call.arguments)))
    except HookVeto as v:
        return ToolResponse(ok=False, error=ToolError(
            kind=ErrorKind.DENIED, tool=call.name,
            message=f"被 hook 拦截: {v.hook_name}", hint=v.reason))
    response = await self._execute(ctx.tool, ctx.args)
    response = await self.hooks.run("post_tool",
            HookContext(tool=ctx.tool, response=response)) or response  # None=不改
    return response
```

为什么难：`or response` 的语义（hook 返回 None=保留原响应）与异常/返回值双通道的边界——每个分支都要测试钉死，这是插件协议的"宪法"，改一字全线回归。

---

## 4. 手敲指引（函数级伪代码）

| 步骤 | 文件 | 函数级作用（伪代码） | 验证 |
|---|---|---|---|
| 1 | `hooks/pipeline.py` | `register：追加+按 priority 排序；run：§1.1 ③ 代码（async_ 后台/veto 抛/modify 链式）；join_background：gather 全部后台任务` | 8 个单测（veto 短路/modify 次序/异步 join） |
| 2 | `pre_tool/permission_hook.py` | `handler：查 M09 gate.check → need_confirm/veto 转 HookResult（ask 也用 veto+reason="需用户确认"——M09 侧已处理交互）；priority=0 最先跑` | 原 M09 权限测试全绿（回归） |
| 3 | `post_tool/format_hook.py` | `handler：tool∈写类且 path.endswith(.gd) → 读盘→简单规则格式化（缩进/空行）→写回→modify 结果摘要；30ms 内完成` | 写丑代码→磁盘已格式化 |
| 4 | `command/` | `Parser：正则 ^/(\w+)\s*(.*)；Registry.dispatch：查表→handler(session)→CommandResult；未知名→difflib 近邻提示；handlers：compact/rewind/skills/plan/model 六个` | 六命令全通+近邻提示 |
| 5 | `skills/loader.py` | `scan：rglob SKILL.md→frontmatter 解析（yaml 头）+正文；catalog_prompt 每技能一行；load：§1.3 ③` | 目录 <300 token |
| 6 | `skills/registry.py`+内置技能 | `skill_search/skill_use 注册为 M04 工具（模型主动）；写 3 个内置技能：打包发布/GDScript 风格/本地化` | 打包任务命中技能 |

---

## 5. 测试与验收

```python
async def test_veto_short_circuits_pipeline():
    # priority 5 的 veto 阻止 priority 10 的 hook 执行（计数器断言）

async def test_modify_chain_order():
    # A(+prefix, p=10) 先于 B(+suffix, p=20)；B 看到 A 改过的参数

async def test_async_hooks_joined_at_session_end():
    # session_end 后所有异步 handler 完成

async def test_unknown_command_suggests_neighbors():
    result = parser.parse("/plans")
    assert "plan" in registry.suggestions("plans")
```

**验收 Demo**：①写"格式化 hook"让 write_script 落盘自动格式化（`ask "把 player.gd 弄乱再看"`）②`/skills` 列目录 → `ask "帮我打包发布 Windows 版"` 触发技能注入、步骤引用检查清单 ③`/compact` 立即压缩（对比走模型的间接性）④`/rewindd` 出近邻提示。

---

## 6. 踩坑记录（留白自填）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

---

## 7. 面试拷打（附详细参考答案）

**1. Hook 管线与 Web 框架 middleware 的同构性？**
答：完全同构。pre_tool 链=请求中间件（进来挨个过：鉴权→审计→参数改写），post_tool 链=响应中间件（出去挨个过：脱敏→格式化）；veto=中间件短路返回 403；modify=中间件改写 request.body 后放行（后续中间件看到改后的）；priority=中间件注册顺序；异步 hook=中间件里的后台任务（fire-and-forget + 结束时 join）。差异只在宿主：middleware 挂 HTTP 生命周期，Hook 挂 Agent 生命周期（工具前后/轮前后/会话前后）。理解锚点：**你会 FastAPI middleware 就已经会 Hook 设计**——这是把成熟 Web 工程经验平移进 Agent 的最佳例子。

**2. 同步/异步 hook 的取舍标准？session_end 为什么要 join？**
答：取舍标准一条：**是否在决策链上**。pre_tool 可能 veto/modify（改变执行结果）→必须同步等结果；统计上报、通知、日志聚合（不改变结果只记录）→异步后台跑，不拖慢管线（每个工具调用都过 hook，10ms×万次=分钟级累积）。join 的原因：异步任务是"发出去了但没等确认完成"——session_end 时若进程直接退出，正在写的审计记录/正在发的通知会丢（数据完整性缺口）；join 保证会话生命周期内所有挂起的后台工作落地。这也是"优雅关闭"（graceful shutdown）的标准组成。

**3. modify 链的执行顺序怎么保证？"后见覆盖先见"怎么治理？**
答：顺序保证：注册时按 priority 排序（小者先），链式传递——每个 modify 后的 ctx 传给下一个 hook。治理"后见覆盖先见"（两个 hook 改同一参数，后面的悄悄覆盖前面的）：①文档约定 priority 段位含义（0-49 系统级/50-99 安全类/100+ 业务类——安全类永远后跑，最终裁决权归安全）；②modify 必须带 reason 进 trace（谁改了什么，可审计）；③测试钉死关键链的组合顺序（permission p=0 → format p=100）。极端情况（两个业务 hook 改同一参数）属于设计冲突，code review 层面拦截。

**4. /rewind 为什么必须是命令而不能"对模型说 rewind"？**
答：确定性清单：①**语义确定**——命令解析是正则，不存在"理解偏差"（模型可能把"我想撤回刚才说的那句话"理解成对话回滚或文件回滚）；②**延迟确定**——0ms 解析直达（模型路径 300ms+ 起步还可能反问）；③**成本确定**——零 token；④**副作用确定**——命令处理器是纯代码，行为可单测（模型路径每次执行可能有差异）；⑤**可达性确定**——模型故障时命令仍可用（逃生舱：/compact /rewind /model 是模型出问题时用户唯一的自救手段）。设计原则：**高危、精确、高频系统操作全部命令化**。

**5. 三类命令的返回处理差异？**
答：direct（查询型：/skills list）：结果直接渲染给用户，不进模型（省一轮调用）；prompt_inject（路由型：/plan 打包）：命令只做"模式切换+任务注入"，实际执行回 Loop（命令是入口不是执行者）；state_change（控制型：/compact /rewind）：直接改会话状态，结果通知模型"上下文已压缩到 X tokens"（让模型知道世界变了，防止它基于被截断的历史困惑）。差异本质：**命令改变的是"谁接下来干活"**——用户自己看（direct）/模型干（inject）/系统干完通知模型（state_change）。

**6. Skills 与记忆的本质区别？**
答：四个维度：内容——Skills 存**方法论**（怎么做打包：步骤/检查清单/坑），记忆存**事实**（发生过什么：偏好/踩坑史/项目状态）；时效——方法论稳定（打包流程 6 个月不变），事实会过时（"项目有 12 个场景"两周后就是错的）；管理——Skills 版本化（v2 替换 v1，git 管理），记忆可遗忘（GC/衰减/软删）；来源——Skills 人工编写/社区贡献（可信度高），记忆自动抽取（要防污染，M08）。一句话：**Skills 是公司的 SOP 文件柜，记忆是员工的工作日志**——前者指导未来，后者记录过去。

**7. 技能目录为什么能压到 300 token？全量常驻的代价？**
答：目录=每技能一行元数据（名称+3 个触发词），20 个技能×15 token≈300。全量常驻的代价算账：20 个技能平均 800 token=16k——每次对话开场就烧掉 16k（M07 预算的 1/8），且大部分对话只用 0~1 个技能（浪费率 >90%）；更糟的是 Lost in the Middle——16k 的技能全文把真正的对话内容挤向低注意力区，模型行为劣化。按需加载把成本从"每对话 16k"降到"用到的对话 +800"——两个数量级的成本差。这是**懒加载思想在上下文工程的应用**（程序按需加载库，不把整个 pypi 塞进内存）。

**8. 触发词误命中的缓解为什么选"模型自己判断"？**
答：三个方案对比：精确匹配（零误命中但漏召回——"帮我弄个安装包"不含"打包"俩字）；模糊匹配/嵌入（召回好但误命中更多）；**目录+模型判断**（触发词进目录只是"提示存在"，最终由模型在对话上下文中判断是否 skill_use）——模型有完整对话语境（用户上文在聊发布流程→"弄个安装包"也能联想到打包技能），误命中率远低于机械匹配，漏召回也低（模型看到目录就知道有什么可用）。本质：**把模式匹配外包给理解能力的持有者**——和 M12 意图分类选 LLM 同一逻辑。代价是每次判断的机会成本（一次工具调用），可接受。

**9. pre_tool hook 与权限 gate 的关系？（先有 gate 后成 hook）**
答：演化关系：M09 的 PermissionGate 先作为 Dispatcher 内的硬编码检查存在（快速满足需求）；M14 把它**正式化**为 priority=0 的 pre_tool hook（包装既有 gate 逻辑）——收益：①权限检查从"内置特权"变成"可被替换/禁用/排序的普通插件"（测试环境禁用权限 hook 一行配置）；②与其他 pre_tool hook（审计、参数改写）统一顺序模型（priority 决定谁先跑）；③第三方可以用自己的权限 hook 替换默认实现。这是重构的经典模式：**特例先落地，通用化后再收编**——先让 M09 快速可用，M14 提供框架后平滑迁入，M09 的测试保证迁移无回归。

**10. 开放题：第三方插件（hook 能改参数能 veto）的安全模型怎么设计？**
答：四层防御：①**能力声明**（capability manifest）——插件打包时声明需要的能力：可读哪些挂载点/能否 modify/veto/需要的工具与路径范围，安装时用户审批声明（像手机 App 权限弹窗）；②**沙箱执行**——第三方 hook 在受限环境跑（子进程/受限 import 白名单），网络与文件访问按声明代理；③**签名与来源**——插件市场分发带签名，安装时校验；未签名插件标记"社区实验性"并默认只给 pass 权限（不能 veto/modify）；④**运行时审计与限速**——所有 modify/veto 进审计日志（谁改了什么参数）；单 hook 执行超时杀除（防恶意阻塞管线）。原则：**默认最小权限，能力可撤销**——与浏览器扩展模型同构（扩展能改请求就会被滥用，Chrome 的做法就是声明+审批+商店审核）。

---

## 8. 教程映射与延伸

- 📘 zero2Agent 08 课（hook & command）
- 必读：Claude Code Hooks 文档（挂载点清单对照）；FastAPI middleware 文档（同构理解）
- 选读：Claude Skills / Cursor Rules 官方说明（两大参考实现）
