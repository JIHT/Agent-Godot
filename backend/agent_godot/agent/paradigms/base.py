"""agent/paradigms/base.py —— 策略基类 + 模式配置 + 注册表（M13 §1.4 / §4 步骤 1）

换挡器本体：四模式不是四套循环，而是同一 ReAct 循环挂不同"契约配置"。

★ 模式层 ⊥ 范式层（M13 §1.3，本模块最易写错的地方）：
- 模式（ModeStrategy）= 产品层的"人机协作契约"，四维差异见下
- 范式（ReAct/Reflection/Plan-and-Solve/Multi-Agent）= 技术层的执行机制，
  通过本模块的钩子挂进来，按需组合启用，**不与模式 1:1 绑定**

四维契约差异（§1.4 ①）：
- tools     → tools_view 裁剪工具视图（registry.filter 是视图不是拷贝）
- 采样参数  → config.temperature / top_p（单一事实源在 models.yaml，此处只做默认）
- 循环钩子  → before_loop / on_tool_done / should_continue（← 范式挂载点）
- 系统提示  → config.system_prompt_template

钩子即范式的挂载点：ask 不重写任何钩子（纯 ReAct 契约）；craft 重写
on_tool_done 挂 Reflection 验证回路；plan 用外循环挂 Plan-and-Solve；
multi 用 should_continue + M15 分发器挂 Multi-Agent 编排。

注册表模式（本项目第三次落地）：新增模式 = 新策略类 + @register，循环零改动。
"""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Literal

from agent_godot.core import Message
from agent_godot.tools import ToolRegistry, ToolResponse


@dataclass
class ModeConfig:
    """一个模式的预置配置。字段即四维差异（见模块 docstring）。"""

    tools: Literal["readonly", "all", "per_worker"] | list[str] = "all"
    temperature: float = 0.7
    top_p: float = 0.95
    verify: None | Literal["L1", "L1+", "L3", "per_task"] = None
    plan_first: bool = False
    system_prompt_template: str | None = None


class ModeStrategy(ABC):
    """策略基类：四钩子全部给默认实现，子类只差异化需要动的部分。

    tools_view 是 classmethod——它只依赖类级 config（静态视图），
    无实例状态；测试里 `PARADIGMS["ask"].tools_view(reg)` 直接类调用。
    """

    mode: str = ""
    config: ModeConfig = ModeConfig()

    def __init__(self, config: ModeConfig | None = None, **kwargs):
        # **kwargs：get_strategy 统一把 llm/loop/runner/approver 传给所有策略，
        # 基类吞掉与本策略无关的项（子类显式声明自己需要的）。
        if config is not None:
            self.config = config

    # ---------- ① 工具集视图 ----------

    @classmethod
    def tools_view(cls, registry: ToolRegistry) -> ToolRegistry:
        """按 config.tools 裁剪工具视图（物理能力边界）。

        - readonly    → 只留只读工具（ask：物理上不给写工具）
        - list[str]   → 白名单视图
        - all/per_worker → 原样返回（per_worker 的分发由 multi/M15 处理）
        """
        cfg = cls.config
        if cfg.tools == "readonly":
            return registry.filter(readonly=True)
        if isinstance(cfg.tools, list):
            view = ToolRegistry()
            for name in cfg.tools:
                if registry.has(name):
                    view.register(registry.get(name))
            return view
        return registry

    # ---------- ② 循环钩子（默认无操作） ----------

    async def before_loop(self, session, task: str) -> list[Message]:
        """循环开始前注入的消息（plan 的系统提示 / multi 的任务拆解）。默认空。"""
        return []

    async def on_tool_done(self, tool: str, resp: ToolResponse,
                           session) -> str | None:
        """工具执行后钩子：返回非 None 时作为 Observation 注入下一轮。
        craft 用它回填 headless 校验错误（客观验证回路）。默认 None。"""
        return None

    async def should_continue(self, session) -> bool:
        """推进控制：plan 的外循环终止条件 / multi 的聚合完成判定。默认 True。"""
        return True


# ---------- 注册表（§2 接口：def register(): PARADIGMS[cls.mode] = cls） ----------

PARADIGMS: dict[str, type[ModeStrategy]] = {}


def register(cls: type[ModeStrategy]) -> type[ModeStrategy]:
    """类装饰器：把策略类登记进 PARADIGMS（按 mode 名索引）。"""
    if not cls.mode:
        raise ValueError(f"策略类 {cls.__name__} 缺少 mode 类属性")
    PARADIGMS[cls.mode] = cls
    return cls


def get_strategy(mode: str, **kwargs) -> ModeStrategy:
    """工厂：按模式名实例化策略（loop.run 的装配入口）。"""
    cls = PARADIGMS.get(mode)
    if cls is None:
        raise ValueError(f"未知模式: {mode!r}（可用: {sorted(PARADIGMS)}）")
    return cls(**kwargs)


__all__ = ["ModeConfig", "ModeStrategy", "PARADIGMS", "register",
           "get_strategy"]
