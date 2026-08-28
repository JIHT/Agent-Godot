"""agent/paradigms/craft.py —— craft 模式：执行者契约 + Reflection 验证回路
（M13 §1.2 / §4 步骤 3）

契约档位：全工具 + 操作级审批门（写操作走 M09 确认门），自主干到底。

★ 范式说明（M13 §1.3）：craft ≠ Reflection。craft 是模式（契约），
本文件挂上去的 VerifyLoop 才是 Reflection 范式的一次启用。craft 内部
实际同时跑着：
- ReAct（底座循环，M03 提供）
- Reflection（本文件：写后 headless 校验 → 错误回填 → 返工）
- Plan-and-Solve（⚪ 隐式：模型拿到"加存档系统"这类任务会自主拆解子步骤，
  只是不强制出 DAG、不强制人审批）

车间质检员制度：模型改完代码（写类工具成功落盘）不能直接交货，先过
headless 校验（L1 语法起步，可配到 L3）。不合格 → 错误行号作为 Observation
回填 → 模型返工 → 再验，max_fixes 封顶；连续失败超限 → 升级给用户
（继续修还是 /rewind 回滚）。

关键认知（§1.2 ②）：质检员是仪器不是师傅——用客观验证器（GodotRunner.check）
而非"再问一遍模型"，因为自评像让学生给自己批卷（基本都打 90 分），仪器不说谎。
"""
from __future__ import annotations

from agent_godot.tools import ToolResponse

from .base import ModeConfig, ModeStrategy, register


class VerifyLoop:
    """客观验证回路：写后自动 headless 校验，错误翻译成模型可读的修复指令。

    runner 协议：与 tools/godot/headless.GodotRunner **鸭子类型兼容**——
    有 .available 属性 + async check() -> CheckResult（.ok / .errors）。
    None 或不可用 = 无验证器（单测/沙箱环境），跳过校验。

    ★ 分级校验（M06 §1.5 四级 ↔ M13 §7.7 累积档，见 LEVELS）：
    verify 字段是**累积档**语义（跑到哪一级），不是"只跑哪一级"：
      "L1"  → L1 语法（秒级）
      "L1+" → L1 语法 + L2 导入（+ 资源断链）
      "L3"  → L1 + L2 + L3 测试（+ 逻辑回归）
      None / "per_task" → 不跑（per_task 由 plan/multi 在节点边界显式触发，M15）

    L4（跑场景）有副作用（截图/状态改文件）且需用户确认，**不在 verify
    取值内**，只能由用户显式调用 runner.run_scene()。

    降级原则：runner 未实现某级方法（如单测 FakeRunner 只有 check）→
    跳过该级继续下一级，不报错。鸭子类型兼容优先于严格契约——这样同一份
    VerifyLoop 既能跑真实 GodotRunner（四级齐全），也能跑极简假 runner。
    """

    WRITE_TOOLS = {"write_file", "godot_write_script", "godot_edit_scene"}

    # 累积档 → 要跑的级别序列（M06 四级的原子级名）
    LEVELS: dict[str, tuple[str, ...]] = {
        "L1": ("L1",),
        "L1+": ("L1", "L2"),
        "L3": ("L1", "L2", "L3"),
    }

    # 逐级定义：(级别, runner 方法名, 人读标签)。顺序 = 执行顺序，
    # 与 M06 §7.6 一致——L1 快失败先跑，L2 必须早于引用它的校验（否则假错）。
    _STEPS = (("L1", "check", "语法"),
              ("L2", "import_assets", "资源导入"),
              ("L3", "run_tests", "测试"))

    def __init__(self, runner=None, max_fixes: int = 3,
                 verify: str | None = "L1"):
        self.runner = runner
        self.max_fixes = max_fixes
        self.verify = verify
        self.fixes = 0

    async def after_write(self, tool: str, resp: ToolResponse,
                          session) -> str | None:
        """返回"未通过的错误摘要"或 None（全部通过 / 不适用）。Loop 注入下一轮。

        只验成功的写（§1.2 伪代码：tool not in WRITE_TOOLS or not resp.ok →
        直接放行）；按 verify 累积档逐级跑，任一级失败即累计 fixes 并返回
        修复指令；全部通过才放行。超限返回升级给人的话术。
        """
        if tool not in self.WRITE_TOOLS or not resp.ok:
            return None
        if self.runner is None or not getattr(self.runner, "available", False):
            return None                                   # 无验证器：跳过

        wanted = self.LEVELS.get(self.verify or "", ())
        if not wanted:
            return None                                   # 未配分级（None/per_task）：不验

        for level, method, label in self._STEPS:
            if level not in wanted:
                continue
            run = getattr(self.runner, method, None)
            if run is None:
                continue                                  # runner 未实现该级：降级跳过
            result = await run()
            if result.ok:
                continue                                  # 本级通过 → 进下一级
            self.fixes += 1
            errs = self._format_errors(result)
            if self.fixes > self.max_fixes:
                return ("验证连续失败超限，请向用户确认：继续修复或 /rewind 回滚。"
                        f"最后一次错误（{level} {label}）：\n{errs}")
            return (f"⚠️ 写入后 headless 校验未通过"
                    f"（{level} {label}校验，第{self.fixes}次），请修复后重写：\n"
                    f"{errs}")
        return None

    @staticmethod
    def _format_errors(result) -> str:
        """CheckResult → 模型可读摘要（带 file:line 定位，修复信号强）。"""
        errs = "\n".join(f"- {e['file']}:{e['line']} {e['msg']}"
                         for e in getattr(result, "errors", [])[:8])
        if not errs:
            errs = (getattr(result, "output", "") or "").strip()[-500:]
        return errs[:1500]


@register
class CraftStrategy(ModeStrategy):
    mode = "craft"
    config = ModeConfig(tools="all", temperature=0.1, verify="L1+")

    def __init__(self, runner=None, max_fixes: int = 3,
                 config: ModeConfig | None = None, **kwargs):
        super().__init__(config, **kwargs)
        # 分级档位从策略配置流入验证回路（ModeConfig.verify → VerifyLoop.verify）。
        # 注意 super().__init__ 才把 config 装上，故必须在它之后读 self.config。
        self.verify_loop = VerifyLoop(runner, max_fixes=max_fixes,
                                      verify=self.config.verify)

    async def on_tool_done(self, tool: str, resp: ToolResponse,
                           session) -> str | None:
        return await self.verify_loop.after_write(tool, resp, session)


__all__ = ["CraftStrategy", "VerifyLoop"]
