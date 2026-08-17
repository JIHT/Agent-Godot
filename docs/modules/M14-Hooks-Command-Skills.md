# M14 Hooks · Command · Skills（可插拔扩展三件套）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 10 · 里程碑 MI-4「完整 Agent 形态」上半场 |
| 代码落点 | `backend/agent_godot/hooks/`（pipeline/pre_tool/post_tool）+ `command/`（parser/handlers）+ `skills/`（loader/registry/builtin） |
| 前置模块 | M09（权限 gate 就是第一个 pre-tool hook 的正式化）· M03（Hook 管线挂在 Dispatcher） |
| 手写比例 | 100% 手写 |
| 教程映射 | 📘 zero2Agent 08 课 · 📝笔记 Hooks/Skills · Claude Code 扩展机制文档 |

---

## 0. 本模块在项目中的位置

到这里 Agent 的"核心功能"全齐了，本模块回答工程可持续性问题：**如何让别人（和三个月后的你）不改核心代码就加能力？** 三件套三个口：

```text
Hook     挂进执行管线的"事件钩子"   —— 横切关注（审计/格式化/统计）
Command  用户的确定性入口           —— /plan /compact /rewind（不经过模型，直达功能）
Skill    领域知识的按需注入         —— "打包发布"技能文档，用到才加载进上下文
```

**交付后状态**：写一个"GDScript 格式化" hook（post_tool 自动格式化写过的 .gd）；新增 `/checkpoint save` 命令零核心改动；`skills/打包发布.md` 让 Agent 突然"会"导出模板——三件套全部插件化落地。

---

## 1. 知识点详解

### 1.1 Hook 管线：优先级排序的同步/异步执行

**① 原理**

Hook = 挂在固定挂载点的回调链。挂载点矩阵（本项目 6 个）：

```text
pre_tool    工具执行前   可否决/改参数   —— 权限门(M09)、参数审计、路径改写
post_tool   工具执行后   可改写结果      —— 自动格式化、敏感信息脱敏、统计
pre_loop    每轮推理前   可注入消息      —— 上下文告警（"本轮预算剩 20%"）
post_loop   每轮推理后   —— 死循环检测上报
session_start / session_end                            —— 记忆召回注入 / 记忆抽取触发(M08)
```

执行模型的两难：**同步 hook（pre_tool）必须快**（每个工具调用都过，10ms×每次=可感延迟）；**异步 hook（统计/通知）不许阻塞管线**。方案：hook 声明 `async_ = True` 时扔进后台任务组，管线不等待，但 session_end 时 join 全部完成（防丢数据）。

```python
@dataclass
class HookSpec:
    name: str
    point: Literal["pre_tool", "post_tool", "pre_loop", "post_loop",
                   "session_start", "session_end"]
    priority: int = 100                  # 小者先执行（同 point 内）
    async_: bool = False                 # True=后台执行不阻塞
    handler: Callable[[HookContext], Awaitable[HookResult | None]]

@dataclass
class HookResult:
    action: Literal["pass", "modify", "veto"] = "pass"
    modified_args: dict | None = None    # modify 时的新参数
    reason: str = ""                     # veto 理由（进审计日志）
```

**② 演进**：函数内直接写横切逻辑（散落各处）→ 装饰器（挂载点受限）→ 显式管线（Web 框架 middleware 的直系亲戚：Express/Koa/FastAPI 的 middleware 链就是 hook 管线）。理解锚点：**pre_tool 链 = FastAPI 的 request middleware，post_tool 链 = response middleware**。

**③ 最小案例**：管线执行核心（veto 短路 + modify 链式传递）

```python
class HookPipeline:
    def __init__(self): self._hooks: dict[str, list[HookSpec]] = defaultdict(list)
    def register(self, spec: HookSpec):
        self._hooks[spec.point].append(spec)
        self._hooks[spec.point].sort(key=lambda s: s.priority)   # 每次注册后保持有序

    async def run(self, point: str, ctx: HookContext) -> HookContext:
        for spec in self._hooks[point]:
            if spec.async_:
                self._bg.create_task(spec.handler(ctx))          # 后台不等待
                continue
            result = await spec.handler(ctx)
            if result is None: continue
            if result.action == "veto":
                raise HookVeto(spec.name, result.reason)          # 短路抛出（调用方转 ToolResponse）
            if result.action == "modify":
                ctx = ctx.evolve(args=result.modified_args)       # 链式：后续 hook 看到新参数
        return ctx
```

**④ 易错点**
- modify 链的"后见覆盖先见"：priority 顺序即参数演化顺序，文档必须写明（不然两个改参数的 hook 互相踩）
- veto 的异常要被 Dispatcher 捕获转成 `ok=False` 的 ToolResponse（进 M04 协议），直接上抛会炸 Loop
- 异步 hook 里不能用已释放的资源（session 关闭后还在写会话对象）——session_end 的 join 兜底由此而来

### 1.2 Command：不经过模型的确定性入口

**① 原理**

`/plan` `/compact` `/rewind 3` `/skills list`——**斜杠命令绕过 LLM 直达功能**。为什么需要：确定性（/rewind 不该"可能"执行）、零成本（不花 token）、低延迟（不等待推理）。解析与分发：

```text
输入以 "/" 开头 → parser 提取 (name, args)
  → 注册表查 handler：内置命令 / 会话命令 / 未知（未知时提示近邻："是要 /plan 吗？"）
  → handler 执行，产出 CommandResult：
     - direct_result：直接显示（/skills list 的清单）
     - prompt_inject：转成对模型的输入（/plan 把后续文本作为 plan 模式任务）
     - state_change：改会话状态（/compact 触发压缩、/rewind 回滚）
```

三种产出对应三种命令本质：**查询型 / 路由型 / 控制型**。命令参数用简单空格分词（不搞复杂 shell 语法），复杂参数留给自然语言（模型来解析）——命令保持"人类可打"的简单性。

**② 演进**：CLI 时代 flags（git 风格）→ IM 时代斜杠命令（Slack/Discord）→ Agent 时代命令成为"确定性逃生舱"（Claude Code 的 /compact /rewind /model 同思想）。**当模型不确定时，命令是用户手里唯一的确定性把手**。

**③ 最小案例**

```python
class CommandParser:
    CMD = re.compile(r"^/(\w+)(?:\s+([\s\S]*))?$")
    def parse(self, input: str) -> Command | None:
        m = self.CMD.match(input.strip())
        if not m: return None
        return Command(name=m.group(1).lower(), args=(m.group(2) or "").strip())

@register_command("compact")
async def cmd_compact(cmd: Command, session: Session) -> CommandResult:
    await session.compact_now()                       # 直接调 M07 Compressor
    return CommandResult.direct(f"已压缩，当前 {session.context_tokens} tokens")
```

**④ 易错点**
- 命令名与工具名要 namespace 区分（命令是用户入口、工具是模型入口，同名会疯）
- /plan 这类"路由型"命令的 args 为空时要交互追问（"要规划什么任务？"）而非报错
- 命令执行也要过审计（改状态的命令尤其——/rewind 是高危动作，记录操作者与目标）

### 1.3 Skills：知识外化的按需加载

**① 原理**

系统提示塞进所有领域知识 = 窗口爆炸 + 注意力稀释（M07 的 Lost in the Middle）。Skills 机制：**领域知识写成独立文档，用到才注入**：

```text
skills/
├─ 打包发布/SKILL.md        # frontmatter: name/触发词/工具依赖
├─ 性能剖析/SKILL.md
├─ GDScript风格指南/SKILL.md
└─ 多语言本地化/SKILL.md

机制：
1. 每技能的 frontmatter 抽出 (name, triggers[], tools[]) 建"技能目录"（常驻上下文，仅 ~200 token）
2. 用户输入命中触发词 / 模型主动调 skill_use 工具 → 注入该 SKILL.md 全文进本轮上下文
3. 技能文档 = 提示词工程的最佳实践沉淀（怎么打包、 pitfalls、检查清单）
```

Skills 的本质：**把"高频提示词"从用户的记忆负担变成系统的资产库**。对比记忆（M08）：记忆存"发生过的事实"，Skills 存"怎么做某类事的方法论"——事实会过时（可遗忘），方法论稳定（版本化管理）。

**② 演进**：用户手写长提示（每次粘贴）→ 提示词模板库（变量填充）→ Cursor Rules / Claude Skills（触发式注入）→ Agent 自主技能检索（把技能目录做成工具，模型自己查自己载——本项目双轨：触发词自动 + 工具手动）。

**③ 最小案例**：SKILL.md 格式与加载器

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
4. 常见坑：模板未安装（tweb 下载）/ C# 项目要先 build / 资源 remap
```

```python
class SkillLoader:
    def catalog_prompt(self) -> str:            # 常驻的目录（极省 token）
        return "\n".join(f"- {s.name}: {'/'.join(s.triggers[:3])}" for s in self.skills)

    async def load(self, name: str) -> str:     # 按需全文注入
        skill = self.by_name[name]
        return f"<skill name='{skill.name}'>\n{skill.body}\n</skill>"
```

**④ 易错点**
- 触发词误命中（"发布"误触发打包技能）→ 注入浪费；缓解：目录里让**模型自己判断**要不要调 skill_use 工具（触发词只是提示）
- 技能文档冲突（两个技能都说"标准流程"）→ 版本化管理 + 技能内互链（"见 GDScript 风格指南"）
- 技能加载要进 trace 并计 token（它是上下文成本的一部分，M07 的 Skills 分区记账）

### 1.4 三件套的协同：一次 `/plan 打包并发布 Windows 版`

命令解析 `/plan`（确定性入口）→ plan 模式生成 DAG → 步骤"执行打包"运行时 pre_tool hook 注入打包技能上下文（技能挂载点也可以是 hook）→ headless 导出工具执行 → post_tool hook 校验产物存在并记录产物路径。**命令定入口、Hook 做横切、Skills 供知识**——三条扩展轴正交。

---

## 2. 接口设计（完整签名）

```python
# hooks/pipeline.py（1.1 全量已给）
# hooks/pre_tool/permission_hook.py    # M09 gate 的 hook 化封装（priority=0 最先跑）
# hooks/post_tool/format_hook.py       # .gd 自动格式化示例插件
# hooks/post_tool/redact_hook.py       # 输出脱敏（.env 内容打码）

# command/parser.py + handlers/
@dataclass
class Command: name: str; args: str
@dataclass
class CommandResult:
    kind: Literal["direct", "prompt_inject", "state_change"]
    text: str | None; new_mode: str | None
class CommandRegistry:
    def register(self, name: str, handler: CommandHandler) -> None: ...
    async def dispatch(self, cmd: Command, session: Session) -> CommandResult: ...
    def suggestions(self, name: str) -> list[str]: ...        # 未知命令近邻提示

# skills/loader.py + registry.py
@dataclass
class Skill:
    name: str; triggers: list[str]; tools_needed: list[str]
    version: int; body: str; source: Path
class SkillLoader:
    def scan(self, roots: list[Path]) -> list[Skill]: ...     # SKILL.md 发现与解析
    def catalog_prompt(self) -> str: ...
    async def load(self, name: str) -> str: ...
class SkillRegistry:                      # 注册为工具（模型可主动查）
    async def skill_search(self, query: str) -> list[str]: ...
    async def skill_use(self, name: str) -> str: ...
```

## 3. 关键难点参考片段：HookVeto 到 ToolResponse 的翻译

veto 在管线里是异常，到模型那里必须是协议化的 Observation——翻译层在 Dispatcher：

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
    response = await self.hooks.run("post_tool",       # post 可改写 response
                HookContext(tool=ctx.tool, response=response)) or response
    return response
```

为什么难：两处 `await ... or response` 的语义（hook 返回 None=不动原响应）与异常/返回值的边界——管线约定的每个分支都要有测试钉死。

## 4. 手敲指引

| 步骤 | 文件 | 做什么 | 验证 |
|---|---|---|---|
| 1 | hooks/pipeline.py | 注册+优先级+三动作 | 单测 8 个（veto/modify/异步 join） |
| 2 | pre_tool/permission_hook.py | M09 gate 包成 hook | 原有权限测试不回归 |
| 3 | post_tool/format_hook.py | .gd 写后自动格式化 | 写丑代码→磁盘上是格式化的 |
| 4 | command/ | 解析+三类 handler | /compact /rewind /skills 全通 |
| 5 | skills/loader.py | SKILL.md 解析+目录注入 | 目录 <300 token |
| 6 | skills/registry.py | skill_search/skill_use 工具 | 模型自主调技能 |
| 7 | 内置技能 ×3 | 打包/风格/本地化 | 打包任务命中技能 |

## 5. 测试与验收

```python
async def test_veto_short_circuits_pipeline():
    # priority 5 的 veto hook 应阻止 priority 10 的 hook 执行

async def test_modify_chain_order():
    # hook A(+prefix) priority 10, hook B(+suffix) priority 20
    # 断言 A 先执行且 B 看到 A 改过的参数

async def test_async_hooks_joined_at_session_end():
    # session_end 后异步 hook 全部完成（计数器断言）
```

**验收 Demo**：写"格式化 hook"让每次 write_script 落盘自动格式化 → `ask "帮我把 player.gd 弄乱再看"` 观察落盘已格式化；`/skills` 列目录 → `ask "帮我打包发布 Windows 版"` 触发技能注入 → 步骤引用检查清单；`/compact` 立即压缩（对比模型路由的间接性）。

## 6. 踩坑记录（留白）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

## 7. 面试拷打

1. Hook 管线与 Web 框架 middleware 的同构性？pass/modify/veto 对应 middleware 的什么？
2. 同步与异步 hook 的取舍？session_end 为什么要 join？
3. modify 链"后见覆盖先见"怎么文档化与测试？
4. 为什么 /rewind 必须是命令而不能是"对模型说 rewind"？确定性入口的价值清单；
5. 三类命令（查询/路由/控制）的返回处理差异？
6. Skills 与记忆（M08）的本质区别？（方法论 vs 事实；版本化 vs 可遗忘）
7. 技能目录为什么能压到 300 token？全量常驻的代价是什么？
8. 触发词误命中的缓解为什么选"模型自己判断"？
9. pre_tool hook 与权限 gate 的关系？（正式化与封装——先有 gate 后成 hook）
10. 开放题：设计第三方插件的安全模型（hook 能改参数能 veto，恶意插件怎么防？签名/沙箱/能力声明）。

## 8. 教程映射与延伸

- 📘 zero2Agent 08 课（hook & command）
- 必读：Claude Code Hooks 文档（挂载点清单对照）；FastAPI middleware 文档（同构理解）
- 选读：Claude Skills / Cursor Rules 官方说明（两大参考实现）
