"""agent/orchestrator.py —— 包工头：拆解 / 冲突分组 / 并发 / 聚合（M15 §1.3）

四步循环（§1.3 ① 严格定义）：
  ① 拆解 decompose：一次 LLM 调用把任务 → 2~4 个自包含任务书（含验收标准+边界）
  ② 静态冲突检查 resolve_groups：写目标相交的子任务**同组串行**——在派发时
     消灭竞态（编译期检查优于运行时崩溃的同一哲学，§2 问答 3）
  ③ 并发执行 _run_groups：组间并行（asyncio.gather）、组内串行；信号量限并发度
     （每个子代理都在打 LLM API，并发爆表触发限流——M02 令牌桶是全局共享的）
  ④ 聚合 aggregate：报告合并 + 跨任务一致性检查（产出物重叠/空报告/未完成），
     冲突时可选派 verifier 仲裁（§1.3 ③）

失败处置三板斧（§2 问答 5，按失败原因分类路由）：
  - 预算类（max_steps/token/timeout）→ 重派一次（改任务书，加"上一轮中断"说明）
  - 执行性小失败 / 部分完成        → 吞并（报告进聚合，主控自己判断要不要补）
  - 方向性失败（error / 空报告）    → 上抛（落进 conflicts，交回主控/用户决策）

★ multi 模式降级契约（M13 §7.9）：拆解失败（模型没吐出 JSON / 网络挂了）时
自动退化为"单个 coder 子任务"——multi 永远可用，只是并行度降为 1。

================================================================================
第二轮加固（§1.4 静态冲突检查 / §1.6 任务书自包含）
================================================================================
第一步的 resolve_groups 用"写目标**字符串**相交"判冲突，会漏掉三类真实冲突：
目录与文件（`save/` vs `save/manager.gd`）、同一文件的多种写法（`.\\src\\a.gd`
vs `res://src/a.gd`）、以及**未声明**写目标（LLM 漏输出字段 → 空集与谁都不
相交 → 静默并行写）。本轮把它升级为：

  归一化（分隔符/协议前缀/大小写跟随文件系统）→ 前缀树双向包含 → 访问三态
  （READ/WRITE/EXCLUSIVE）→ 七类处置决策树（escalate/serialize/depends/
  contract/merge）→ 按处置结果分组 + 稳定排序

纵深防御四层（§1.4 ③(d)）：
  L0 规划期：DECOMPOSE_PROMPT 强制声明 write_targets（减少冲突**产生**）
  L1 派发前：WriteScope 判定 + 决策树分组（消灭**已知**冲突）★本文件
  L2 运行时：M04 乐观锁 hash → CONFLICT → 重读重改（拦 L1 漏报，已有）
  L3 聚合期：artifacts 重叠检测 → verifier 仲裁 → CheckpointStore 回滚

★ 串行 ≠ 安全（§1.4 ⑤-4/⑤-5）：组内串行只消灭了"同时写"，还要补两件事——
前驱产出注入（`_chain_ctx` 传报告摘要 + 文件 hash，后继才能增量修改而非重写）
与 fail-fast（前驱失败时后继标 blocked 跳过，不许在半成品上继续写）。

★ 判定函数保持**纯函数**（不改外部状态、不发事件），告警收集进 `self.warnings`
由 `run` 统一 emit——async 的 emit 会让判定逻辑不可单测（§3 难点二）。
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import heapq
import json
import logging
import os
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

from agent_godot.core import LLMRequest, Message, Usage
from agent_godot.tools import ToolRegistry

from .subagents import (Constraints, Rule, SubagentSpec, SubtaskResult,
                        load_constraints, spawn)

logger = logging.getLogger(__name__)

# 决策树产出的处置动作（§1.4 ③ 表）
ACTION_SERIALIZE = "serialize"      # 串行（默认安全档）
ACTION_DEPENDS = "depends"          # 升级为数据依赖（串行 + 产出注入）
ACTION_CONTRACT = "contract"        # 契约化重拆（唯一能提并行度的一档）
ACTION_MERGE = "merge"              # 合并成一个子任务（升级模型 + 预算翻倍）
ACTION_ESCALATE = "escalate"        # 上抛用户（受保护文件）
ACTION_ABORT = "abort"              # 中止（CI / 无人值守的 escalate 替身）

# 可重派的停止原因（预算类：任务书没毛病，只是工人体力不够）
RETRYABLE = ("max_steps", "token_budget", "usd_budget", "timeout",
             "loop_detected")

RETRY_HINT = ("\n\n【重派说明】上一轮执行在预算内未跑完就被中断（不是你的错）。"
              "请直接给出：1) 已完成的部分 2) 尚未完成的部分 3) 继续做的最小步骤。"
              "不要重复已完成的工作。")


# ---------- ① 拆解提示（§3：决定 multi 上限的一段 prompt） ----------

DECOMPOSE_PROMPT = """你是任务编排者。把用户任务拆解为 2~4 个子任务，输出 JSON：
[{{"title": "...", "brief": "给子代理的完整任务书：目标+边界(只许动哪些文件/目录)+
   验收标准(可检查的完成判据)", "spec": "explorer|coder|verifier",
   "write_targets": ["..."], "depends": ["其他子任务title或空"]}}]
拆解原则：
- 探查先行：不熟悉项目时第一个子任务必须是 explorer 勘察
- 写目标不相交：两个子任务不许写同一个文件（做不到就串行 depends）
- 验收独立：最后一个子任务建议是 verifier 全局验收
- 每份任务书自包含：子代理看不到全局对话，缺的信息写进 brief
- write_targets 必须**精确到文件路径**（不是目录）；确实拿不准就写 null，
  编排层会按最保守方式处理（宁可串行，不可并行写坏文件）
- 只读子任务（勘察 / 验收）写 `[]`（空数组）——**`[]` 与 null 不等价**：
  `[]` = "我声明不写任何文件"（可并行），null = "我不知道"（按最保守处理）

只输出 JSON（不要多余文字，可用 ```json 围栏）。

用户任务：{task}
项目现状摘要：{digest}"""

# 重拆提示（§1.4 ⑤-7：并行度归零 = 白拆，给一次带 hint 的重拆机会）
REDECOMPOSE_HINT = (
    "\n\n【重拆要求】上一次拆解的子任务**写目标高度重叠**，只能串行执行，"
    "并行度为 1（等于白拆）。请按**职责边界**重新切分：让每个子任务写**互不相交**"
    "的文件；若确实切不开，就合并成 1~2 个子任务并说明理由。")

# 串行链上下文（§1.4 ③(c)：注入前驱产出 + 基线 hash，防语义覆盖）
CHAIN_HINT = """
【串行链上下文】你前面的子任务「{title}」已完成，它对这些文件做了如下改动：
{report_digest}
当前文件基线 hash: {hashes}
请**基于上述现状增量修改**，不要重写整个文件（重写会丢失前驱的改动）。
"""

# 契约化重拆（§1.4 ③(b)）：共享文件降级为双方只读的契约，各写各的
CONTRACT_BRIEF = (
    "【契约化重拆】契约文件 `{seam}` 已起草完成：你**只读**它（禁止修改），"
    "并**只写** `{target}`（不要动其他文件，尤其是原来那个共享文件——装配由后续"
    "子任务负责）。\n\n")


# ---------- ② 冲突判定原语（§1.4 ③(a)，纯函数，可单测） ----------

class Access(str, Enum):
    """访问模式三态（§1.4 ①-2）。

    READ      只读：与任何访问都不冲突（读-读、读-写都不冲突）
    WRITE     写  ：与同路径 / 祖先 / 后代路径的 WRITE 冲突
    EXCLUSIVE 受保护文件（入口 / 配置 / migration / 锁文件）：与一切冲突
    """

    READ = "read"
    WRITE = "write"
    EXCLUSIVE = "excl"


@dataclass(frozen=True)
class WriteScope:
    """归一化后的写作用域：路径前缀树上的一个节点（§1.4 ③(a)）。

    归一化铁律（跨平台 + 抗幻觉）：
      1. 分隔符统一 POSIX——win32 的 '\\' 必须吃（本项目开发环境即 win32）
      2. 剥离 res:// user:// 等协议前缀（Godot 与裸路径是同一文件）
      3. 大小写折叠**跟随目标文件系统**：NTFS 不敏感 → 折叠，ext4 敏感 → 保留。
         折叠策略与目标不一致 = 同一份编排代码在两个平台判定结果不同
         ——这是最隐蔽的一类"跨平台竞态"
      4. 目录形态统一补尾斜杠，否则 'save' 会误判为 'savesettings' 的祖先
    """

    norm: str
    access: Access = Access.WRITE

    @classmethod
    def of(cls, raw: str, access: Access = Access.WRITE,
           case_insensitive: bool = False) -> "WriteScope":
        p = _norm_path(raw, case_insensitive)
        return cls(p + "/" if _is_dir(raw) else p, access)

    def conflicts_with(self, other: "WriteScope") -> bool:
        if self.access is Access.READ or other.access is Access.READ:
            return False                    # 读-读、读-写都不冲突（写-读走 depends）
        if Access.EXCLUSIVE in (self.access, other.access):
            return True                     # 受保护文件：无条件冲突
        return _covers(self.norm, other.norm) or _covers(other.norm, self.norm)


def _norm_path(raw: str, case_insensitive: bool = False) -> str:
    """路径归一化（不含目录尾斜杠规则——glob 名单不能补斜杠，否则匹配不上）。"""
    p = str(raw or "").strip().replace("\\", "/")
    for scheme in ("res://", "user://", "file://"):
        if p.lower().startswith(scheme):
            p = p[len(scheme):]
            break
    # 只剥单个 './' 前缀——不能用 lstrip("./")：它会连剥并把 '/abs/x' 的
    # 首斜杠也吃掉，把绝对路径变成相对路径（归一化把语义改了比不改更糟）
    p = p[2:] if p.startswith("./") else p
    p = p.lstrip("/").rstrip("/")
    return p.lower() if case_insensitive else p


def _covers(a: str, b: str) -> bool:
    """a 覆盖 b：相等，或 a 是 b 的祖先目录（'save/' ⊇ 'save/manager.gd'）。"""
    return a == b or b.startswith(a if a.endswith("/") else a + "/")


def _is_dir(raw: str) -> bool:
    """目录判定：尾斜杠，或无扩展名（'save'、'save/'、'res://save' 都是目录）。"""
    p = str(raw or "").strip().replace("\\", "/")
    for scheme in ("res://", "user://", "file://"):
        if p.lower().startswith(scheme):
            p = p[len(scheme):]
            break
    if not p:
        return False
    if p.endswith("/"):
        return True
    return "." not in p.rsplit("/", 1)[-1]


@dataclass
class Conflict:
    """一对子任务之间的冲突（§1.4 ② 接口）。

    kind  : write_write | protected | undeclared | write_read
    action: 决策树产出（serialize|depends|contract|merge|escalate|abort），
            由 `plan_conflicts` 回填（判定与处置分离，两段都可单测）
    """

    a: str                                  # 冲突的两个子任务 title
    b: str
    scope: str = ""                         # 冲突路径（归一化后）
    kind: str = "write_write"
    action: str = ""

    def describe(self) -> str:
        return (f"冲突[{self.kind}]：{self.a} × {self.b}"
                f"（{self.scope or '*'}）→ {self.action or '未处置'}")


# 冲突类型的严重度（一对子任务间取最严重的那一档）
_SEVERITY = {"write_read": 0, "write_write": 1, "undeclared": 2, "protected": 3}

# merge 档最多重判几轮（合并后的子任务可能与第三方产生新冲突）
_MAX_MERGE_PASSES = 4


# ---------- ③ 数据结构 ----------

@dataclass
class Subtask:
    """一个子任务：任务书 + 角色 + 写目标 + 依赖（编排层的工单）。

    ★ write_targets 是**三态**，不是二态（§1.4 ①-4，本节最危险的一类漏判）：
        None       = 未声明（知识缺失）→ 按最保守分支处理（fail-safe 串行）
        set()      = 声明只读 → 与任何写目标都不冲突（可并行）
        {"a.gd"}   = 声明写   → 按前缀树判定冲突
      **未声明 ≠ 只读**：把"未知"当"无"是 fail-open，会让两个子任务静默并行
      写同一个文件——不报错、不重试、只交出一份看起来合理的半成品。

    ★ 例外（不是 fail-open）：角色的工具白名单**物理上全只读**时，它不可能
      产生写-写冲突，未声明可安全地按"声明只读"处理。这是白名单推出的硬结论
      （不是猜），所以只对"有可能写"的角色才走 fail-safe。
    """

    title: str = ""
    task_brief: str = ""
    spec: SubagentSpec | None = None       # 显式指定时优先（否则按 spec_name 查表）
    spec_name: str = "coder"
    write_targets: set[str] | None = None
    depends: list[str] = field(default_factory=list)
    retries: int = 0
    access: Access = Access.WRITE          # 该子任务的默认访问模式（§1.4 ①-2）
    tolerate_upstream_failure: bool = False  # True=前驱失败时自己照跑（默认 fail-fast）
    seam: str = ""                         # 契约文件（contract 档的接缝声明）
    seam_target: str = ""                  # 重拆后自己负责的那个文件
    barrier: bool = False                  # 屏障步骤：后继走"组间等待"而非合并同组


@dataclass
class OrchestrResult:
    """编排结果：聚合报告 + 每子任务 usage 汇总 + 冲突清单。

    stop_reason / steps 与 M03 LoopResult 同名字段——cli 打印与前端渲染
    不必区分"单代理跑完"和"编排跑完"两种结果类型（鸭子类型兼容）。
    """

    task: str = ""
    report: str = ""
    results: list[SubtaskResult] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=lambda: Usage(0, 0))
    groups: list[list[str]] = field(default_factory=list)
    arbitration: str = ""
    stop_reason: str = "natural"
    task_id: str = ""                       # §1.4 ⑥-8：检查点槽位（/rewind 用它）
    warnings: list[tuple] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results) and not self.conflicts

    @property
    def steps(self) -> int:
        return len(self.results)


# ---------- 受保护文件名单（§4 步骤 8） ----------

PROTECTED_CONFIG_PATHS = (
    Path("config/protected.yaml"),
    Path("../config/protected.yaml"),
    Path("../../config/protected.yaml"),
)

# 内置兜底名单（读不到配置时的安全默认值——是"最小保护"而不是"不保护"）
DEFAULT_PROTECTED = (
    "project.godot", "*.godot", "export_presets.cfg",
    "uv.lock", "poetry.lock", "package-lock.json", "*.lock",
    "migrations/**", ".agent_godot/**",
)


def load_protected(config_path=None) -> tuple[list[str], str]:
    """读 `config/protected.yaml` → (受保护路径名单, on_protected 策略)。

    读不到 / 解析失败 → 回落内置名单 + ask（**配置缺失不该变成"无保护"**——
    与 M09 权限"默认最严"同一取向）。
    """
    path = Path(config_path) if config_path is not None else None
    if path is None:
        path = next((p for p in PROTECTED_CONFIG_PATHS if p.exists()), None)
    if path is None or not path.exists():
        return list(DEFAULT_PROTECTED), "ask"
    try:
        import yaml
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:                  # noqa: BLE001 —— 配置坏了用默认值
        logger.warning("读取 protected.yaml 失败，回落内置名单: %s", e)
        return list(DEFAULT_PROTECTED), "ask"
    items = data.get("protected") or data.get("paths") or []
    names = [str(x).strip() for x in items if str(x).strip()]
    action = str(data.get("on_protected") or "ask").strip().lower()
    if action not in ("ask", "serialize", "abort"):
        action = "ask"
    return (names or list(DEFAULT_PROTECTED)), action


# ---------- ④ 编排器 ----------

class Orchestrator:
    """包工头：拆解 → 冲突判定 → 决策树处置 → 并发派发 → 聚合交付。"""

    def __init__(self, llm, specs: dict[str, SubagentSpec],
                 registry: ToolRegistry | None = None, bus=None, *,
                 max_parallel: int = 3, max_retries: int = 1,
                 auto_arbitrate: bool = True, fallback_spec: str = "coder",
                 model: str = "orchestrator", session_ctx: dict | None = None,
                 project_root=None, case_insensitive: bool | None = None,
                 protected: list[str] | None = None, on_protected: str = "ask",
                 protected_config=None, allow_contract: bool = False,
                 max_chain: int = 4, checkpoints=None, approver=None,
                 constraints: Constraints | None = None, redecompose=None):
        self.llm = llm
        self.specs = dict(specs)
        self.registry = registry
        self.bus = bus
        self.max_parallel = max(1, max_parallel)
        self.max_retries = max(0, max_retries)
        self.auto_arbitrate = auto_arbitrate
        self.fallback_spec = fallback_spec
        self.model = model                      # 记账用标记（非真实模型名）
        self.session_ctx = dict(session_ctx or {})
        if project_root is not None:
            self.project_root = Path(project_root)
        else:                                   # session_ctx 是次要来源
            hinted = self.session_ctx.get("project_root")
            self.project_root = Path(hinted) if hinted else None

        # ---- §1.4 冲突判定配置 ----
        # 大小写折叠**跟随文件系统语义**（NTFS 不敏感 / ext4 敏感）——折叠策略
        # 与目标不一致 = 同一份编排代码跨平台判定结果不同（§1.4 ⑤-2）
        self.case_insensitive = (os.name == "nt") if case_insensitive is None \
            else case_insensitive
        self.protected, cfg_action = (load_protected(protected_config)
                                      if protected is None
                                      else (list(protected), on_protected))
        if protected is None:
            on_protected = cfg_action
        self.protected_norms = [
            _norm_path(p, self.case_insensitive) for p in self.protected]
        # 无人值守（CI / A2A 自动化）没有应答者：确认门 = 永久挂起，强制 abort
        self.unattended = bool(os.environ.get("CI")
                               or os.environ.get("AGENT_GODOT_UNATTENDED"))
        self.on_protected = "abort" if self.unattended else on_protected
        if self.unattended and on_protected == "ask":
            logger.info("无人值守环境：受保护文件处置由 ask 强制降级为 abort")
        self.allow_contract = allow_contract   # 激进档做成显式开关（§1.4 ⑤-8）
        self.max_chain = max(1, max_chain)     # 串行链长度上限（§1.4 ⑥-4）
        self.approver = approver               # escalate 的确认门回调（可 async）
        self.redecompose = redecompose         # 体检时的重拆通道（可 async）
        self.checkpoints = checkpoints         # M06 TaskCheckpoints（可回滚）

        # ---- §1.6 约定传递 ----
        self._constraints = constraints
        self.warnings: list[tuple] = []
        self.escalations: list[Conflict] = []
        self.blocked: set[str] = set()
        self.decompose_calls = 0
        self._sem: asyncio.Semaphore | None = None
        self._group_waits: dict[int, set[int]] = {}
        self._by_title: dict[str, Subtask] = {}
        self._ranks: dict[str, int] = {}

    @property
    def constraints(self) -> Constraints:
        """项目硬约定（懒加载：没显式注入就按项目根读 constraints.md）。"""
        if self._constraints is None:
            self._constraints = load_constraints(self.project_root)
        return self._constraints

    # ---------- 主入口 ----------

    async def run(self, session, task: str,
                  subtasks: list[Subtask] | None = None) -> OrchestrResult:
        """任务 → 编排结果（multi 模式的外循环，类比 plan 的 run_plan_mode）。

        进主控会话的只有两样：**用户任务**与**聚合报告**——子代理的过程
        消息一条都不进（loop.run 的记账习惯在此保持一致，否则 /rewind
        回放时会缺"用户到底要了什么"这一环）。

        subtasks：跳过拆解直接派发（测试 / 外部已有工单时的注入口）。

        步骤（§1.3 ①）：拆解 → 体检（并行度归零则重拆/降级）→ 分组 → 上抛裁决
        → 并发派发 → 聚合。第一步就开检查点槽（§1.4 ⑥-8：可回滚是最后一道保险）。
        """
        self.warnings.clear()
        self.escalations.clear()
        self.blocked.clear()
        append = getattr(session, "append", None)
        if callable(append):
            append(Message(role="user", content=task))
        digest = self._digest(session)
        if subtasks is None:
            subtasks = await self.decompose(task, digest=digest)
        task_id = self._open_checkpoint(subtasks)
        groups = await self._health_check(subtasks, task, digest)
        subtasks = [s for g in groups for s in g]
        await self._emit("orchestrator_plan", task=task,
                         subtasks=[s.title for s in subtasks],
                         groups=[[s.title for s in g] for g in groups])
        await self._flush_warnings()
        await self._resolve_escalations()

        ctx = self._spawn_ctx(digest)
        results = await self._run_groups(groups, ctx)

        order = {s.title: i for i, s in enumerate(subtasks)}
        results.sort(key=lambda r: order.get(r.title, len(order)))

        out = await self.aggregate(results, task=task)
        out.groups = [[s.title for s in g] for g in groups]
        out.task_id = task_id
        out.warnings = list(self.warnings)
        if task_id:
            out.report += (f"\n\n# 回滚\n- 检查点 task_id: `{task_id}`"
                           f"（`/rewind {task_id}` 可回到编排前；"
                           "冲突未消解时**默认保留现场**供人工处置）")
        # 主控上下文只收**聚合报告**（不是各子代理的过程数据——隔离的命门）
        if callable(append) and out.report:
            append(Message(role="assistant", content=out.report))
        await self._emit("orchestrator_done", ok=out.ok,
                         subtasks=len(out.results),
                         conflicts=len(out.conflicts),
                         tokens=out.usage.input_tokens + out.usage.output_tokens,
                         task_id=task_id)
        return out

    # ---------- ① 拆解 ----------

    async def decompose(self, task: str, digest: str = "",
                        hint: str = "") -> list[Subtask]:
        """任务 → 子任务清单（一次 LLM 调用，输出 JSON）。

        拆解失败（模型吐不出 JSON / 调用异常）→ 降级为单 coder 子任务：
        multi 模式永远可用，最坏情况退化为"单代理跑一次"（M13 降级契约）。

        hint：体检发现"并行度归零"时的重拆要求（§1.4 ⑤-7，追加在用户任务后）。
        """
        self.decompose_calls += 1
        prompt = DECOMPOSE_PROMPT.format(
            task=task + (hint or ""), digest=digest or "（无）")
        try:
            resp = await self.llm.complete(LLMRequest(
                model=self.model,
                messages=[Message(role="user", content=prompt)],
                temperature=0.2, stream=False))
            parsed = self._parse_subtasks(resp.content if resp else "")
        except Exception as e:                  # noqa: BLE001 —— 拆解失败不许炸
            logger.warning("任务拆解失败，降级为单子任务: %s", e)
            await self._emit("error", error=f"任务拆解失败: {e}")
            parsed = []
        return parsed or [Subtask(title=_short(task),
                                  task_brief=task,
                                  spec_name=self.fallback_spec)]

    def _parse_subtasks(self, content: str) -> list[Subtask]:
        """JSON → Subtask 列表（容忍围栏/前后废话/字段缺失）。

        ★ write_targets 的三态在这里定生死（§1.4 ⑤-3，本节最危险的一类漏判）：
          字段**缺失** → `None`（未声明，走 fail-safe 串行）
          空数组 `[]`  → `set()`（声明只读，可并行）
          有值          → 该集合
        上一版把缺失当成 `[]`，等于把"未知"当"无"——fail-open，两个子任务会
        静默并行写同一个文件，而交付报告写"全部完成"。
        """
        data = _load_json(content)
        if isinstance(data, dict):
            data = data.get("subtasks") or data.get("tasks") or []
        if not isinstance(data, list):
            return []
        out: list[Subtask] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or f"子任务{i + 1}").strip()
            brief = str(item.get("brief") or item.get("task_brief")
                        or item.get("description") or "").strip()
            if not brief:
                continue
            out.append(Subtask(
                title=title, task_brief=brief,
                spec_name=str(item.get("spec") or self.fallback_spec).strip(),
                write_targets=self._parse_targets(item),
                depends=[str(d) for d in _as_list(item.get("depends"))],
                access=self._parse_access(item),
                seam=str(item.get("seam") or "").strip(),
                seam_target=str(item.get("seam_target") or "").strip()))
        return out

    @staticmethod
    def _parse_targets(item: dict) -> set[str] | None:
        """write_targets 三态解析：字段缺失 / null → None，[] → set()。"""
        if "write_targets" not in item:
            return None
        raw = item.get("write_targets")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        return {str(t) for t in _as_list(raw) if str(t).strip()}

    @staticmethod
    def _parse_access(item: dict) -> Access:
        """access 字段 → Access（只读子任务显式声明 read 才能与写任务并行）。"""
        raw = str(item.get("access") or "").strip().lower()
        if raw in ("read", "readonly", "read_only", "ro"):
            return Access.READ
        if raw in ("excl", "exclusive", "protected"):
            return Access.EXCLUSIVE
        return Access.WRITE

    # ---------- ② 静态冲突检查：写目标分组（§1.3 ③） ----------

    def resolve_groups(self, subtasks: list[Subtask]) -> list[list[Subtask]]:
        """冲突判定 → 决策树处置 → 按处置结果分组（§1.4 ③）。

        组语义仍是**组间并行、组内串行**，但"什么必须同组"由决策树说了算，
        而不是只看写目标字符串是否相交：

            serialize（含 undeclared / protected 回落）→ 同组排队
            depends   → 转成数据依赖后同组（前驱在前，且产出注入后继）
            merge     → 合成**一个**子任务（升级模型档位 + 预算翻倍）
            contract  → 契约先行，重拆成各写各的（唯一能提并行度的一档）
            escalate  → 上抛用户（受保护文件）；无应答 = abort（不派发）

        ★ 依赖必须**合并进同一组**（不能只放到"更晚的组"）：组间是并发的，
          组下标更大 ≠ 执行更晚——两个组会同时开跑，依赖者会读到未生成的文件
          （§6 踩坑记录 2026-08-28 第一条）。

        ★ 稳定排序（§1.4 ⑤-6）：拓扑序优先，`title` 字典序做 tie-break。
          否则同一任务两次运行分组结果不同，测试无法对拍、线上无法复现。
        """
        self.warnings.clear()                   # 以**定稿那一轮**的告警为准
        work = list(subtasks)
        # ① merge 档：合并后的子任务可能引入新冲突，最多再判几轮
        for _ in range(_MAX_MERGE_PASSES):
            mark = len(self.warnings)
            self._index(work)
            planned = self.plan_conflicts(self.detect_conflicts(work))
            target = next((c for c in planned if c.action == ACTION_MERGE), None)
            del self.warnings[mark:]            # 中间轮的告警不算数
            if target is None:
                break
            work = self._apply_merge(work, target)

        # ② 定稿：再判一次，按 action 建并查集 + 依赖表
        self._index(work)
        conflicts = self.plan_conflicts(self.detect_conflicts(work))
        depends: dict[str, set[str]] = {
            s.title: {d for d in s.depends if d in self._by_title}
            for s in work}
        union = _UnionFind(s.title for s in work)
        self.escalations.clear()

        for c in conflicts:
            if c.action in (ACTION_SERIALIZE, ACTION_MERGE):
                union.union(c.a, c.b)
            elif c.action == ACTION_DEPENDS:
                union.union(c.a, c.b)           # 依赖也要同组（组间是并发的）
                reader, writer = self._rw_pair(c)
                depends[reader.title].add(writer.title)
            elif c.action == ACTION_CONTRACT:
                if self._try_contract(work, c, depends):
                    continue                    # 切开了 → 各写各的（保持并行）
                union.union(c.a, c.b)           # 切不开 → 老实落回串行
            elif c.action in (ACTION_ESCALATE, ACTION_ABORT):
                self.escalations.append(c)      # 由 run 统一问用户/直接 abort

        # ③ 依赖同样合并进同一组（前驱必须先跑完）
        #    ★ 例外：契约步骤是**屏障**，它的后继走"组间等待"——否则把契约
        #      和两路实现全并进一组，契约化重拆提上来的并行度又被吃回去了
        for title, deps in depends.items():
            for d in deps:
                pre = self._by_title.get(d)
                if pre is not None and pre.barrier:
                    continue
                union.union(title, d)

        # ④ 组件 → 组（组内拓扑序 + 字典序 tie-break），组间按 (深度, 首 title)
        buckets: dict[str, list[Subtask]] = {}
        for st in work:
            buckets.setdefault(union.find(st.title), []).append(st)
        groups = [self._topo(members, depends) for members in buckets.values()]
        self._compute_ranks(work, depends)
        groups.sort(key=lambda g: (min(self._ranks.get(s.title, 0) for s in g),
                                   g[0].title))
        self._group_waits, groups = self._build_waits(groups, depends)

        # ⑤ 串行链过长提示（§1.4 ⑥-4：链式依赖 > 4 本质上不适合并行）
        for g in groups:
            if len(g) > self.max_chain:
                self.warnings.append(("chain_too_long", g[0].title))
        return groups

    # ---------- ②-a 冲突判定（纯函数，可单测） ----------

    def detect_conflicts(self, subtasks: list[Subtask]) -> list[Conflict]:
        """两两判定写作用域冲突（§1.4 ③(a)）。**纯函数**：不改外部状态、不发事件。

        判定链：三态声明 → 归一化（分隔符/协议前缀/大小写）→ 前缀树双向包含
        → 访问三态 → 受保护名单升级为 EXCLUSIVE。
        """
        self._index(subtasks)
        out: list[Conflict] = []
        for i, a in enumerate(subtasks):
            for b in subtasks[i + 1:]:
                c = self._pair_conflict(a, b)
                if c is not None:
                    out.append(c)
        return out

    def _pair_conflict(self, a: Subtask, b: Subtask) -> Conflict | None:
        sa, sb = self._scopes(a), self._scopes(b)
        if sa is None:                          # 未声明（知识缺失）→ fail-safe
            return Conflict(a=a.title, b=b.title, scope="*", kind="undeclared")
        if sb is None:
            return Conflict(a=b.title, b=a.title, scope="*", kind="undeclared")
        best: tuple[str, str] | None = None
        for x in sa:
            for y in sb:
                kind = self._scope_kind(x, y)
                if kind is None:
                    continue
                scope = x.norm if len(x.norm) <= len(y.norm) else y.norm
                if best is None or _SEVERITY[kind] > _SEVERITY[best[0]]:
                    best = (kind, scope)
        if best is None:
            return None
        return Conflict(a=a.title, b=b.title, scope=best[1], kind=best[0])

    @staticmethod
    def _scope_kind(x: WriteScope, y: WriteScope) -> str | None:
        """两个作用域的关系（前提：已确认路径相交）。"""
        if not (_covers(x.norm, y.norm) or _covers(y.norm, x.norm)):
            return None
        if x.access is Access.READ and y.access is Access.READ:
            return None                         # 读-读：天然并行（处置 #6）
        if Access.EXCLUSIVE in (x.access, y.access):
            return "protected"                  # 受保护文件：与一切冲突（处置 #3）
        if x.access is Access.READ or y.access is Access.READ:
            return "write_read"                 # 写-读：升级为数据依赖（处置 #5）
        return "write_write"

    def _scopes(self, st: Subtask) -> list[WriteScope] | None:
        """子任务 → 归一化作用域列表。None = 未声明（fail-safe 信号）。"""
        if st.write_targets is None:
            # 未声明 ≠ 只读；但**物理只读**的角色不可能产生写-写冲突——这是
            # 白名单给出的硬结论而非猜测，所以只对"有可能写"的角色 fail-safe
            return [] if self._is_physically_readonly(st) else None
        scopes: list[WriteScope] = []
        for raw in st.write_targets:
            ws = WriteScope.of(raw, access=st.access,
                               case_insensitive=self.case_insensitive)
            if st.access is not Access.READ and self._is_protected(ws):
                ws = WriteScope(ws.norm, Access.EXCLUSIVE)
            scopes.append(ws)
        return scopes

    def _is_protected(self, scope: WriteScope) -> bool:
        """命中受保护名单（精确 / 前缀 / glob 三种写法都认）。"""
        for pattern in self.protected_norms:
            if _covers(pattern, scope.norm) or _covers(scope.norm, pattern):
                return True
            if fnmatch.fnmatch(scope.norm, pattern):
                return True
        return False

    def _is_physically_readonly(self, st: Subtask) -> bool:
        """角色的工具白名单里**一个写工具都没有**（空白名单也算——无工具=无写）。"""
        spec = st.spec or self.specs.get(st.spec_name)
        if spec is None or spec.is_remote:
            return False                        # 查不到 / 远程工人：不推定安全
        try:
            for name in spec.tools.names():
                if not spec.tools.get(name).meta.readonly:
                    return False
        except Exception:                       # noqa: BLE001 —— 查不动就当不安全
            return False
        return True

    # ---------- ②-b 处置决策树（纯函数，可单测） ----------

    def plan_conflicts(self, conflicts: list[Conflict]) -> list[Conflict]:
        """给每条冲突打处置标签（§1.4 ③ 决策树，默认保守档）。**纯函数**。

        路由顺序即"风险从低到高"（处置表 §1.4 ①）：
          protected  → escalate（受保护文件，人说了算；无人值守 → abort）
          undeclared → serialize（知识缺失，宁可慢不可错）+ 记一条拆解质量告警
          write_read → depends  （转成数据依赖，串行 + 前驱产出注入）
          write_write:
              接缝可切分 → contract（契约化重拆，唯一能提并行度的一档）
              重叠不可分 → merge  （合成一个子任务，升级模型 + 预算翻倍）
              其余       → serialize（默认安全档）

        ★ 本函数**不发事件**：async 的 emit 会让"纯判定逻辑"不可单测。
          告警收集到 self.warnings，由调用方（run）统一 emit。
        """
        for c in conflicts:
            if c.kind == "protected":
                c.action = ACTION_ESCALATE if self.on_protected == "ask" \
                    else self.on_protected
            elif c.kind == "undeclared":
                c.action = ACTION_SERIALIZE
                self.warnings.append(("undeclared_write_targets", c.a))
                # ↑ 拆解质量可观测指标：该指标持续偏高说明 DECOMPOSE_PROMPT 要迭代
            elif c.kind == "write_read":
                c.action = ACTION_DEPENDS
            else:                                       # write_write
                c.action = (ACTION_CONTRACT if self._has_seam(c)
                            else ACTION_MERGE if self._overlap_ratio(c) > 0.5
                            else ACTION_SERIALIZE)
        return conflicts

    def _has_seam(self, c: Conflict) -> bool:
        """接缝是否可切分（§1.4 ⑤-8：判错了代价比判对保守大，故默认关闭）。

        判据是**显式声明**的：两边都声明同一个契约文件 `seam` 且各自声明了
        `seam_target`（重拆后自己负责的文件）。说不清接缝就返回 False，落回
        串行——宁可慢，不可两个 coder 一起跑偏。
        """
        if not self.allow_contract:
            return False
        a, b = self._by_title.get(c.a), self._by_title.get(c.b)
        if a is None or b is None:
            return False
        return bool(a.seam and b.seam and a.seam == b.seam
                    and a.seam_target and b.seam_target
                    and a.seam_target != b.seam_target)

    def _overlap_ratio(self, c: Conflict) -> float:
        """写目标重叠度（>0.5 判为"同一区域、不可切分" → merge）。"""
        sa = self._scopes(self._by_title[c.a]) if c.a in self._by_title else None
        sb = self._scopes(self._by_title[c.b]) if c.b in self._by_title else None
        if not sa or not sb:
            return 0.0
        hits = sum(1 for x in sa for y in sb
                   if _covers(x.norm, y.norm) or _covers(y.norm, x.norm))
        return hits / max(len(sa), len(sb))

    def _rw_pair(self, c: Conflict) -> tuple[Subtask, Subtask]:
        """写-读对的 (reader, writer)。"""
        a, b = self._by_title.get(c.a), self._by_title.get(c.b)
        if a is None or b is None:
            return a, b
        return (a, b) if a.access is Access.READ else (b, a)

    def _apply_merge(self, subtasks: list[Subtask],
                     c: Conflict) -> list[Subtask]:
        """merge 档：两个子任务合成一个（升级模型档位 + 预算翻倍）。

        为什么是"合并"而不是"串行"：改动重叠度高时，两次读写的上下文割裂
        比一次做完更容易出错，且省掉一次任务书交接（§1.4 处置 #2）。
        """
        a, b = self._by_title.get(c.a), self._by_title.get(c.b)
        if a is None or b is None:
            return subtasks
        merged = Subtask(
            title=f"{a.title}+{b.title}",
            task_brief=(f"【合并自两个写目标高度重叠的子任务】\n\n"
                        f"## {a.title}\n{a.task_brief}\n\n## {b.title}\n"
                        f"{b.task_brief}\n\n【要求】两部分必须**在同一次实现里一并"
                        f"完成**，保持命名与风格一致，不要分两次改。"),
            spec=self._upgraded_spec(a, b),
            spec_name=a.spec_name,
            write_targets=(a.write_targets | b.write_targets)
            if a.write_targets is not None and b.write_targets is not None else None,
            depends=sorted({d for d in list(a.depends) + list(b.depends)
                            if d not in (a.title, b.title)}),
            access=Access.EXCLUSIVE if Access.EXCLUSIVE in (a.access, b.access)
            else Access.WRITE)
        return [merged] + [s for s in subtasks
                           if s.title not in (a.title, b.title)]

    def _upgraded_spec(self, a: Subtask, b: Subtask) -> SubagentSpec | None:
        """合并档的模型与预算：取推理档 + 预算翻倍（§1.4 处置 #2）。"""
        specs = [s for s in (a.spec or self.specs.get(a.spec_name),
                             b.spec or self.specs.get(b.spec_name))
                 if s is not None]
        if not specs:
            return None
        base = next((s for s in specs if "reason" in s.model.lower()), specs[0])
        bigger = max(specs, key=lambda s: s.budget.steps)
        return replace(base,
                       budget=replace(bigger.budget,
                                      steps=bigger.budget.steps * 2,
                                      tokens=bigger.budget.tokens * 2,
                                      usd=bigger.budget.usd * 2))

    def _try_contract(self, subtasks: list[Subtask], c: Conflict,
                      depends: dict[str, set[str]]) -> bool:
        """contract 档：起草契约 → 各写各的（并行度 1 → 2）。

        只在"能明确说出接缝在哪"时切开（§1.4 ⑤-8）。切开后两个子任务的
        write_targets 变成各自的 `seam_target`，前缀树判定自然不再冲突。
        """
        a, b = self._by_title.get(c.a), self._by_title.get(c.b)
        if a is None or b is None:
            return False
        seam = a.seam
        step = Subtask(
            title=f"起草契约 {seam}",
            task_brief=(f"只写**一个**文件 `{seam}`：把下面两个子任务共同依赖的"
                        f"接口签名、常量、错误码枚举定义清楚。不写实现、不引入依赖、"
                        f"不改任何其他文件。\n\n- {a.title}：{a.task_brief[:200]}\n"
                        f"- {b.title}：{b.task_brief[:200]}"),
            spec_name=a.spec_name,
            write_targets={seam},
            access=Access.WRITE,
            barrier=True)
        subtasks.append(step)
        self._by_title[step.title] = step        # 立刻入索引：屏障判定要用
        for st in (a, b):
            st.task_brief = CONTRACT_BRIEF.format(
                seam=seam, target=st.seam_target) + st.task_brief
            st.write_targets = {st.seam_target}
            depends.setdefault(st.title, set()).add(step.title)
        return True

    # ---------- ②-c 体检：并行度归零 = 白拆 ----------

    def run_health_check(self, subtasks: list[Subtask]) -> list[list[Subtask]]:
        """分组后体检（§1.4 ⑤-7）：只有 1 组 = 白拆，重拆一次，仍为 1 则降级。

        `decompose_calls` 语义：本轮编排已发生的拆解次数。入参这批 subtasks
        本身来自一次拆解（此处补计），重拆再加一次——**最多重拆一次**，
        因为重拆的收益递减而成本线性增长。

        ★ 只体检"**因冲突**而串行"的白拆：一条纯依赖链（explorer → coder →
        verifier）天然只有 1 组，但它买的是**隔离**而不是并行（§1.1 两大动机
        之一），降级成单 coder 反而把隔离收益一起丢了。冲突导致的串行才是
        "并行度归零"，那才是拆解质量差的信号。
        """
        needed, groups = self._needs_redecompose(subtasks)
        if not needed:
            return groups
        retry = self.redecompose(subtasks, REDECOMPOSE_HINT) \
            if self.redecompose is not None else None
        self.decompose_calls += 1               # 注入的重拆通道自己不计数
        return self._finish_health_check(retry, subtasks)

    async def _health_check(self, subtasks: list[Subtask], task: str,
                            digest: str = "") -> list[list[Subtask]]:
        """`run` 走的体检入口（多了"用 LLM 重拆一次"这条异步通道）。"""
        needed, groups = self._needs_redecompose(subtasks)
        if not needed:
            return groups
        retry = None
        if self.redecompose is not None:            # 注入的重拆通道优先
            retry = self.redecompose(subtasks, REDECOMPOSE_HINT)
            if asyncio.iscoroutine(retry):
                retry = await retry
            self.decompose_calls += 1               # 该通道自己不计数
        elif self.llm is not None and task:         # 否则自己带 hint 重拆一次
            retry = await self.decompose(task, digest=digest,
                                         hint=REDECOMPOSE_HINT)  # decompose 自计数
        return self._finish_health_check(retry, subtasks)

    def _needs_redecompose(self, subtasks: list[Subtask]
                           ) -> tuple[bool, list[list[Subtask]]]:
        """是否需要重拆（只有"因冲突而单组"才算白拆）。"""
        self.decompose_calls = max(self.decompose_calls, 1)
        groups = self.resolve_groups(subtasks)
        if len(groups) > 1 or len(subtasks) <= 1:
            return False, groups
        if not self.detect_conflicts(subtasks):     # 纯依赖链：不算白拆
            return False, groups
        return True, groups

    def _finish_health_check(self, retry, fallback: list[Subtask]
                             ) -> list[list[Subtask]]:
        """重拆结果的收尾：并行度上来了就用它，否则降级（不白付拆解开销）。"""
        if not retry:
            groups = self._degrade(fallback)
        else:
            retry_groups = self.resolve_groups(retry)
            groups = retry_groups if len(retry_groups) > 1 \
                else self._degrade(retry)
        # ★ 告警必须放在**最后一次 resolve_groups 之后**——它开头会 clear
        #   warnings（以定稿那轮为准），先记会被吃掉
        self.warnings.append(("parallelism_one", fallback[0].title))
        return groups

    def _degrade(self, subtasks: list[Subtask]) -> list[list[Subtask]]:
        """降级：所有子任务合成一个 coder 工单（并行度归零不如不拆）。"""
        brief = "\n\n".join(f"## {s.title}\n{s.task_brief}" for s in subtasks)
        targets: set[str] | None = set()
        for s in subtasks:
            if s.write_targets is None:
                targets = None                  # 有一个未声明 → 保持未声明
                break
            targets |= set(s.write_targets)
        merged = Subtask(title=_short(subtasks[0].title if subtasks else "任务"),
                         task_brief=brief or "（空任务）",
                         spec_name=self.fallback_spec,
                         write_targets=targets, depends=[])
        return [[merged]]

    # ---------- ②-d 排序与依赖（确定性） ----------

    def _index(self, subtasks: list[Subtask]) -> None:
        self._by_title = {s.title: s for s in subtasks}

    @staticmethod
    def _topo(subtasks: list[Subtask],
              depends: dict[str, set[str]]) -> list[Subtask]:
        """稳定拓扑序（Kahn + 堆）：入度相同时按 **title 字典序**出队。

        ★ 确定性是可复现与测试对拍的前提（§1.4 ⑤-6）：若 tie-break 依赖输入
        顺序，同一任务两次运行的分组与执行顺序就会不同。
        """
        by_title = {s.title: s for s in subtasks}
        titles = sorted(by_title)                   # 字典序做底噪
        deps = {t: sorted(d for d in depends.get(t, ()) if d in by_title)
                for t in titles}
        indeg = {t: len(deps[t]) for t in titles}
        children: dict[str, list[str]] = {t: [] for t in titles}
        for t, ds in deps.items():
            for d in ds:
                children[d].append(t)
        heap = [t for t in titles if indeg[t] == 0]
        heapq.heapify(heap)
        order: list[Subtask] = []
        while heap:
            u = heapq.heappop(heap)
            order.append(by_title[u])
            for v in children[u]:
                indeg[v] -= 1
                if indeg[v] == 0:
                    heapq.heappush(heap, v)
        if len(order) < len(titles):            # 有环：剩下的按字典序补（尽力而为）
            done = {s.title for s in order}
            order.extend(by_title[t] for t in titles if t not in done)
        return order

    def _compute_ranks(self, subtasks: list[Subtask],
                       depends: dict[str, set[str]]) -> None:
        """依赖深度（组间排序用，拓扑序上的最长路径长度）。"""
        self._ranks: dict[str, int] = {}
        for st in self._topo(subtasks, depends):
            deps = [d for d in depends.get(st.title, ()) if d in self._ranks]
            self._ranks[st.title] = (max(self._ranks[d] for d in deps) + 1) \
                if deps else 0

    def _build_waits(self, groups: list[list[Subtask]],
                     depends: dict[str, set[str]]
                     ) -> tuple[dict[int, set[int]], list[list[Subtask]]]:
        """跨组依赖 → 组等待表（契约先行这类"组间屏障"用）。

        正常情况下依赖已并进同组，这里只剩契约步骤的跨组屏障。万一等待关系
        成环（不该发生），退化为"全部并成一组串行"——环路等待会死锁。
        """
        where = {s.title: i for i, g in enumerate(groups) for s in g}
        waits: dict[int, set[int]] = {i: set() for i in range(len(groups))}
        for i, group in enumerate(groups):
            for st in group:
                for d in depends.get(st.title, ()):
                    j = where.get(d)
                    if j is not None and j != i:
                        waits[i].add(j)
        if _has_cycle(waits):
            flat = self._topo([s for g in groups for s in g], depends)
            return {}, [flat]
        return waits, groups

    # ---------- ③ 并发派发 ----------

    async def _run_groups(self, groups: list[list[Subtask]],
                          ctx: dict) -> list[SubtaskResult]:
        """组间并行、组内串行；信号量把同时在跑的子代理压到 max_parallel。

        ★ 并发上限的维度是 **LLM 不是 CPU**（§1.4 ⑥-3）：信号量必须落在
        **每个子任务**上（不是每组），因为每个子代理都在打 API，而 M02 的
        令牌桶是全局共享的。

        ★ 组等待（契约先行的跨组屏障）：有 `waits` 的组等前驱组 set 之后
        再开工。`_build_waits` 已保证无环，不会死锁。
        """
        self._sem = asyncio.Semaphore(self.max_parallel)
        done = [asyncio.Event() for _ in groups]

        async def run_group(index: int, group: list[Subtask]) -> list[SubtaskResult]:
            try:
                for pre in sorted(self._group_waits.get(index, ())):
                    await done[pre].wait()
                return await self._run_group(group, ctx)
            finally:
                done[index].set()               # 异常也不能把后继吊死

        gathered = await asyncio.gather(
            *(run_group(i, g) for i, g in enumerate(groups)),
            return_exceptions=True)
        results: list[SubtaskResult] = []
        for group, item in zip(groups, gathered):
            if isinstance(item, BaseException):
                # 单个子代理炸了不能掀桌子：转成失败报告进聚合（§1.3 失败处理）
                for st in group:
                    results.append(SubtaskResult(
                        spec_name=st.spec_name, ok=False, title=st.title,
                        report=f"子代理执行异常: {type(item).__name__}: {item}",
                        stop_reason="error"))
            else:
                results.extend(item)
        return results

    async def _run_group(self, group: list[Subtask],
                         ctx: dict) -> list[SubtaskResult]:
        """组内串行：写目标相交 / 依赖链。两条铁律：产出注入 + fail-fast。

        铁律一（§1.4 ⑤-4）：**串行 ≠ 安全**。组内只保证"不并发写"，后继不知道
        前驱改了什么，很可能重新读文件按自己理解重写一遍，把前驱改动整段抹掉
        ——所以每个后继的任务书都要注入前驱的报告摘要 + 变更文件的最新 hash，
        并显式要求"增量修改而非重写"。

        铁律二（§1.4 ⑤-5）：**前驱失败不许继续写**。上游预算耗尽会留下半成品
        文件，下游接着在上面改产出的脏数据，比明明白白的失败难排查十倍——
        除非工单显式声明 `tolerate_upstream_failure`。

        ★ `tolerate_upstream_failure` 属于**后继**（它声明"上游挂了我也能干"）：
        默认全部后继标 blocked；声明了容忍的照跑（但拿不到前驱的产出注入）。
        每个位置都留一行结果——聚合层按位置对齐，不因为跳过就少一行。
        """
        out: list[SubtaskResult] = []
        chain = dict(ctx)
        poisoned: set[str] = set()
        for i, st in enumerate(group):
            if st.title in poisoned and not st.tolerate_upstream_failure:
                out.append(SubtaskResult(
                    spec_name=st.spec_name, ok=False, title=st.title,
                    stop_reason="blocked",
                    report="被上游失败阻塞，已跳过——避免在半成品上继续写"))
                continue
            async with self._limit():           # 并发度上限（防 LLM 限流）
                res = await self._run_subtask(st, chain)
            out.append(res)
            if res.ok:
                chain = self._chain_ctx(chain, res)
            else:
                poisoned.update(p.title for p in group[i + 1:])
        return out

    def _limit(self) -> asyncio.Semaphore:
        """并发度闸门（懒建：`_run_group` 允许被单独调用做单测）。"""
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_parallel)
        return self._sem

    def _chain_ctx(self, ctx: dict, done: SubtaskResult) -> dict:
        """把前驱的产出摘要 + 文件基线 hash 注入后继的执行上下文（§1.4 ③(c)）。

        hash 还有第二个用途：后继带着它去写，M04 的乐观锁能一次命中，省掉
        CONFLICT 重读重改的那一轮。
        """
        out = dict(ctx)
        hashes = dict(ctx.get("file_hashes") or {})
        hashes.update(done.file_hashes or {})
        digests = list(ctx.get("chain_digests") or [])
        summary = " ".join((done.report or "").split())[:300]
        digests.append(
            f"「{done.title or done.spec_name}」（{'成功' if done.ok else '未完成'}）："
            f"{summary}"
            + (f"；产出 {', '.join(done.artifacts[:8])}" if done.artifacts else ""))
        out["file_hashes"] = hashes
        out["chain_digests"] = digests
        out["chain_hint"] = CHAIN_HINT.format(
            title=done.title or done.spec_name,
            report_digest="\n".join(f"  - {d}" for d in digests),
            hashes=", ".join(f"{p}={h}" for p, h in sorted(hashes.items()))
            or "（无）")
        return out

    async def _run_subtask(self, st: Subtask, ctx: dict) -> SubtaskResult:
        """派一个工单（含 escalate 拦截与重派一次的重试策略）。"""
        if st.title in self.blocked:
            return SubtaskResult(spec_name=st.spec_name, ok=False, title=st.title,
                                 report="受保护文件未获批准，已跳过",
                                 stop_reason="blocked")
        spec = st.spec or self.specs.get(st.spec_name) or \
            self.specs.get(self.fallback_spec) or _first_spec(self.specs)
        if spec is None:
            raise RuntimeError("没有任何可用子代理角色（specs 为空）")
        await self._emit("subtask_start", title=st.title, spec=spec.name,
                         write_targets=sorted(st.write_targets or []))
        result = await spawn(spec, st.task_brief, ctx)
        result.title = st.title
        result.file_hashes = self._hash_targets(result.artifacts)
        while (not result.ok and result.stop_reason in RETRYABLE
               and st.retries < self.max_retries):
            st.retries += 1
            await self._emit("subtask_retry", title=st.title,
                             attempt=st.retries, reason=result.stop_reason)
            result = await spawn(spec, st.task_brief + RETRY_HINT, ctx)
            result.title = st.title
            result.attempts = st.retries + 1
            result.file_hashes = self._hash_targets(result.artifacts)
        await self._emit("subtask_done", title=st.title, spec=spec.name,
                         ok=result.ok, stop_reason=result.stop_reason,
                         attempts=result.attempts)
        return result

    def _hash_targets(self, paths: list[str]) -> dict[str, str]:
        """变更文件的内容指纹（串行链传递基线用）——读不到就算了，不阻断。"""
        if not paths or self.project_root is None:
            return {}
        out: dict[str, str] = {}
        for rel in paths[:20]:
            try:
                p = (self.project_root / str(rel).replace("\\", "/"))
                if not p.exists():
                    continue
                out[str(rel)] = hashlib.sha256(
                    p.read_bytes()).hexdigest()[:16]
            except (OSError, ValueError):
                continue
        return out

    # ---------- ④ 聚合 ----------

    async def aggregate(self, results: list[SubtaskResult],
                        task: str = "") -> OrchestrResult:
        """报告合并 + 一致性检查（冲突可触发 verifier 仲裁）。

        聚合**不是拼接**：报告矛盾（一个说完成一个说部分完成）、产出物撞车
        必须显式列出，静默拼接 = 埋雷（§1.3 易错点③）。

        两类检查分开放（§1.6）：
          - `find_conflicts`    ：产出物重叠 / 空报告 / 未完成（L3 拦截 L2 漏报）
          - `check_constraints` ：自报假设 × 项目硬约定（拦"静默补全"）
        """
        conflicts = self.find_conflicts(results)
        conflicts.extend(self.check_constraints(results))
        usage = Usage(
            input_tokens=sum(r.usage.input_tokens for r in results),
            output_tokens=sum(r.usage.output_tokens for r in results),
            cost_usd=round(sum(r.usage.cost_usd for r in results), 6))
        arbitration = ""
        if conflicts and self.auto_arbitrate and self.llm is not None \
                and "verifier" in self.specs:
            try:
                arbitration = await self.arbitrate(results, conflicts)
            except Exception as e:              # noqa: BLE001 —— 仲裁失败不拖垮交付
                arbitration = f"（仲裁子代理执行失败: {e}）"
        report = self._render(task, results, conflicts, arbitration, usage)
        return OrchestrResult(
            task=task, report=report, results=list(results),
            conflicts=conflicts, usage=usage, arbitration=arbitration,
            stop_reason="natural" if not conflicts else "partial")

    def check_constraints(self, results: list[SubtaskResult],
                          rules: list[Rule] | None = None) -> list[str]:
        """assumptions × CONSTRAINTS 求交：命中禁止项 → 进 conflicts（§1.6 ③(d)）。

        只查**假设**（任务书没说、子代理自己定的），不查任务书明确要求的东西——
        后者本来就该由 verifier 按验收标准核。

        ★ 冷启动（CONSTRAINTS 为空）：比对无意义，**只自报假设不做比对**，
        并在聚合报告里显式提示这批产出"无约定约束"（§1.6 ⑥-6）——让用户知道
        它们是未经校验的，而不是假装查过了。
        """
        rules = self.constraints.rules if rules is None else list(rules or [])
        if not rules:
            return []
        hits: list[str] = []
        for r in results:
            for a in r.assumptions:
                for rule in rules:
                    if rule.forbids(a):
                        hits.append(f"约定违反：子任务「{r.title or r.spec_name}」"
                                    f"假设「{a}」，与项目约定「{rule.text}」冲突")
        return hits

    def find_conflicts(self, results: list[SubtaskResult]) -> list[str]:
        """跨子任务静态一致性检查（§1.3 ③）：产出物重叠 / 空报告 / 未完成。"""
        conflicts: list[str] = []
        owner: dict[str, list[str]] = {}
        for r in results:
            for artifact in r.artifacts:
                owner.setdefault(artifact, []).append(r.title or r.spec_name)
        for artifact, owners in owner.items():
            if len(owners) > 1:                 # 两个子代理都产出同一文件
                conflicts.append("产出物冲突："
                                 f"{artifact} 被 {' / '.join(owners)} 同时改动")
        for r in results:
            if not r.report.strip():
                conflicts.append(f"子任务「{r.title or r.spec_name}」"
                                 "未产出交付报告（无法验收）")
            elif not r.ok:
                conflicts.append(
                    f"子任务「{r.title or r.spec_name}」未完成"
                    f"（{r.stop_reason}，尝试 {r.attempts} 次）")
        return conflicts

    async def arbitrate(self, results: list[SubtaskResult],
                        conflicts: list[str]) -> str:
        """派 verifier 做一次仲裁（合并还是删一——只给意见，不改文件）。"""
        brief = ("以下是多个子代理的交付报告与发现的冲突，请给出仲裁意见：\n\n"
                 "【冲突清单】\n" + "\n".join(f"- {c}" for c in conflicts)
                 + "\n\n【各子任务报告】\n"
                 + "\n\n".join(f"## {r.title or r.spec_name}（{r.spec_name}）\n"
                               f"{r.report}" for r in results)
                 + "\n\n【要求】输出：1) 冲突定性与优先级 2) 每个冲突的处置建议"
                   "（保留哪个/如何合并/谁返工）3) 最终是否可交付。只给结论，"
                   "不要修改任何文件。")
        result = await spawn(self.specs["verifier"], brief, self._spawn_ctx(""))
        return result.report

    # ---------- 视图与辅助 ----------

    def _render(self, task: str, results: list[SubtaskResult],
                conflicts: list[str], arbitration: str, usage: Usage) -> str:
        lines: list[str] = []
        if task:
            lines.append(f"# 任务\n{task}\n")
        lines.append(f"# 子任务交付（{len(results)} 个）")
        for r in results:
            lines.append(f"- {r.summary()}")
            if r.artifacts:
                lines.append(f"    产出: {', '.join(r.artifacts[:10])}")
            if r.assumptions:
                lines.append(f"    自报假设: {'；'.join(r.assumptions[:5])}")
        lines.append("")
        if conflicts:
            lines.append("# 冲突（需人工确认或返工）")
            lines.extend(f"- {c}" for c in conflicts)
        else:
            lines.append("# 冲突\n- 无（跨子任务一致性检查通过）")
        # 约定冷启动提示（§1.6 ⑥-6）：没登记约定就不能假装查过了
        if self.constraints.empty:
            lines.append("\n# 约定\n- 本项目未登记硬性约定，以下产出"
                         "**未经约定校验**（可在 `.agent_godot/constraints.md` 登记）")
        if arbitration:
            lines.append(f"\n# 仲裁意见（verifier）\n{arbitration}")
        if self.warnings:
            lines.append("\n# 告警")
            lines.extend(f"- {k}: {v}" for k, v in self.warnings)
        total = usage.input_tokens + usage.output_tokens
        lines.append(f"\n# 用量\n- token: {usage.input_tokens} 入 + "
                     f"{usage.output_tokens} 出 = {total}"
                     f"（成本约 ${usage.cost_usd:.4f}）")
        return "\n".join(lines)

    async def _flush_warnings(self) -> None:
        """统一 emit 判定阶段收集的告警（判定函数保持纯函数，§3 难点二）。"""
        for kind, detail in self.warnings:
            await self._emit("orchestrator_warn", kind=kind, detail=detail)

    async def _resolve_escalations(self) -> None:
        """受保护文件的确认门（§1.4 处置 #3 / ⑥-5）。

        交互式 → 问 `approver`；无人值守（CI / A2A 自动化）→ 已在 __init__ 把
        `on_protected` 强制成 abort——**没人应答的确认门等于永久挂起**。
        拿不到批准就整组不派发（标 blocked），绝不"先斩后奏"。
        """
        for c in self.escalations:
            question = (f"子任务「{c.a}」与「{c.b}」都要改动受保护文件 "
                        f"`{c.scope}`，并行/串行都可能破坏它。是否允许继续？")
            allowed = False
            if self.approver is not None:
                try:
                    ret = self.approver(question, c)
                    allowed = await ret if asyncio.iscoroutine(ret) else bool(ret)
                except Exception as e:          # noqa: BLE001 —— 门坏了按拒绝处理
                    logger.warning("受保护文件确认门异常，按拒绝处理: %s", e)
                    allowed = False
            await self._emit("orchestrator_escalation", question=question,
                             allowed=bool(allowed), scope=c.scope)
            if not allowed:
                self.blocked.update({c.a, c.b})
                self.warnings.append(("protected_blocked", c.scope))

    def _open_checkpoint(self, subtasks: list[Subtask] | None) -> str:
        """编排开始前建 task 级快照（§1.4 ⑥-8：可回滚是最后一道保险）。

        ★ **冲突无法自动消解时默认保留现场**（用户可能想人工救）——所以这里只
        开槽 + 快照声明的写目标，绝不自动回滚；能不能退回去交给用户一句
        `/rewind {task_id}`。
        """
        if self.checkpoints is None or self.project_root is None:
            return ""
        try:
            task_id = self.checkpoints.open_task()
            for st in subtasks or []:
                for raw in sorted(st.write_targets or ()):
                    path = self.project_root / str(raw).replace("\\", "/")
                    for scheme in ("res://", "user://"):
                        if str(raw).lower().startswith(scheme):
                            path = self.project_root / str(raw)[len(scheme):]
                            break
                    if path.is_file():
                        self.checkpoints.snapshot(path, reason="orchestrator")
            return task_id
        except Exception as e:                  # noqa: BLE001 —— 快照失败不阻断编排
            logger.warning("开启编排检查点失败（本轮不可回滚）: %s", e)
            return ""

    def _digest(self, session) -> str:
        """项目现状摘要（§3 的 digest 参数）：主控对话尾 + 可用工具清单。

        这是"任务书自包含"的补漏通道（§2 问答 8 防漏三招之一）——子代理
        看不到主控对话，约定类信息只能靠这里显式传递。
        """
        parts: list[str] = []
        msgs = list(getattr(session, "messages", None) or [])
        tail = [m for m in msgs if m.role in ("user", "assistant")][-6:]
        if tail:
            parts.append("最近对话要点:\n" + "\n".join(
                f"- {m.role}: {' '.join((m.content or '').split())[:200]}"
                for m in tail))
        if self.registry is not None:
            names = self.registry.names()
            parts.append("可用工具: " + ", ".join(names[:40])
                         + ("…" if len(names) > 40 else ""))
        return "\n".join(parts)

    def _spawn_ctx(self, digest: str) -> dict:
        """子代理执行上下文：bus / digest / **CONSTRAINTS** / 项目根。

        ★ CONSTRAINTS 在这里无条件注入（§1.6 ③(b)）——约定不进主控的脑子也能
        到子代理手里，这是"治本"的那一招；且对**所有角色一视同仁**（verifier
        也拿同一份），否则验收标准与 brief 同源 = 一起漏。
        """
        ctx = dict(self.session_ctx)
        ctx.setdefault("bus", self.bus)
        if digest:
            ctx["digest"] = digest
        ctx["constraints"] = self.constraints
        if self.project_root is not None:
            ctx.setdefault("project_root", self.project_root)
        return ctx

    async def _emit(self, type_: str, **payload) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.emit(type_, **payload)
        except Exception:                       # noqa: BLE001 —— 事件不许拖垮执行
            pass


# ---------- 小工具 ----------

def _load_json(text: str):
    """从模型输出里抠出 JSON（容忍 ```json 围栏 / 前后废话）。"""
    raw = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        start, end = raw.find("["), raw.rfind("]")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
        else:
            start, end = raw.find("{"), raw.rfind("}")
            if start < 0 or end <= start:
                raise ValueError(f"模型输出不是合法 JSON: {raw[:120]!r}")
            raw = raw[start:end + 1]
    return json.loads(raw)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value] if str(value).strip() else []


class _UnionFind:
    """并查集：把"必须同组串行"的子任务并成一个组件。"""

    def __init__(self, titles):
        self._parent = {t: t for t in titles}

    def find(self, x: str) -> str:
        root = x
        while self._parent.get(root, root) != root:
            root = self._parent[root]
        while self._parent.get(x, x) != root:      # 路径压缩
            self._parent[x], x = root, self._parent.get(x, x)
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # 稳定：永远把字典序大的挂到小的下面（与排序 tie-break 同向）
        if rb < ra:
            ra, rb = rb, ra
        self._parent[rb] = ra


def _has_cycle(waits: dict[int, set[int]]) -> bool:
    """组等待关系是否成环（成环会死锁，调用方需退化为全串行）。"""
    state: dict[int, int] = {}                    # 0=访问中 1=已完成

    def dfs(node: int) -> bool:
        if state.get(node) == 1:
            return False
        if state.get(node) == 0:
            return True                           # 回边 → 有环
        state[node] = 0
        if any(dfs(n) for n in sorted(waits.get(node, ()))):
            return True
        state[node] = 1
        return False

    return any(dfs(n) for n in sorted(waits))


def _short(text: str, limit: int = 30) -> str:
    one_line = " ".join((text or "").split())
    return one_line[:limit] or "主任务"


def _first_spec(specs: dict[str, SubagentSpec]) -> SubagentSpec | None:
    return next(iter(specs.values()), None)


__all__ = ["DECOMPOSE_PROMPT", "REDECOMPOSE_HINT", "CHAIN_HINT",
           "OrchestrResult", "Orchestrator", "Subtask", "Conflict",
           "WriteScope", "Access", "RETRYABLE", "RETRY_HINT",
           "load_protected", "DEFAULT_PROTECTED"]
