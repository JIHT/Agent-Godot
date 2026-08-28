"""skills/registry.py —— 把 Skills 接到模型手上（M14 §4 步骤 6）

目录常驻解决"知道有什么"，模型主动检索解决"用哪个"：
- skill_search：按任务描述找技能（只回目录行，不回全文——省 token）
- skill_use   ：按名称取全文（真正注入上下文的那一步，进 trace 计 token）

为什么注册成 M04 工具而不是塞进系统提示（§1.3 ④演进）：工具化之后模型
能**自己决定何时取手册**，而不是人（或触发词正则）替它决定；这是从
"提示词模板库"到"Agent 自主技能检索"的分界线。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from agent_godot.tools import (BaseTool, ErrorKind, ToolError, ToolMeta,
                               ToolRegistry, ToolResponse)

from .loader import SkillLoader

CATALOG_TAG = "<skills-catalog>"


class SkillSearchTool(BaseTool):
    """按任务描述检索可用技能（只回目录行，需要全文再用 skill_use 取）。"""
    meta = ToolMeta(name="skill_search",
                    description="按任务描述检索可用技能（返回命中的技能名与简介，"
                                "需要完整方法论时再用 skill_use 加载全文）",
                    readonly=True, risk="low", tags={"skill"})

    class Params(BaseModel):
        query: str = Field(description="任务描述或关键词，如 '打包发布 Windows 版'")

    def __init__(self, loader: SkillLoader):
        self.loader = loader

    async def run(self, query: str) -> ToolResponse:
        hits = self.loader.match(query)
        if not hits:
            return ToolResponse(
                ok=True,
                summary=f"没有技能匹配 {query!r}。全部技能目录:\n"
                        f"{self.loader.catalog_prompt() or '（空）'}",
                data={"hits": 0})
        lines = [f"- {s.name}（v{s.version}）: {s.description or '无描述'}"
                 for s in hits]
        return ToolResponse(
            ok=True,
            summary="命中技能（用 skill_use 加载全文后按步骤执行）:\n"
                    + "\n".join(lines),
            data={"hits": len(hits), "names": [s.name for s in hits]})


class SkillUseTool(BaseTool):
    """按名称加载技能全文（按需注入本轮上下文——Skills 机制的最后一公里）。"""
    meta = ToolMeta(name="skill_use",
                    description="按名称加载技能全文（方法论/检查清单/常见坑），"
                                "加载后严格按其中的步骤执行当前任务",
                    readonly=True, risk="low", tags={"skill"})

    class Params(BaseModel):
        name: str = Field(description="技能名，如 '打包发布'（可用 skill_search 查）")

    def __init__(self, loader: SkillLoader):
        self.loader = loader

    async def run(self, name: str) -> ToolResponse:
        try:
            body = await self.loader.load(name)
        except KeyError as e:
            from .loader import SkillLoader
            near = SkillLoader.nearest(self.loader.names(), name)
            hint = f"近邻: {', '.join(near)}" if near else "用 skill_search 检索"
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.NOT_FOUND, tool="skill_use", message=str(e),
                hint=f"{hint}，或输入 /skills list 看完整目录"))
        return ToolResponse(ok=True, summary=body, data={"name": name})


def register_skill_tools(registry: ToolRegistry,
                         loader: SkillLoader) -> SkillLoader:
    """把技能两件套注册进工具注册表（skills 与 godot 工具一样显式注册）。"""
    registry.register(SkillSearchTool(loader))
    registry.register(SkillUseTool(loader))
    return loader


def install_skills(registry: ToolRegistry, loader: SkillLoader | None = None,
                   session=None, roots: list | None = None) -> SkillLoader:
    """一键装配：扫描 → 注册工具 →（可选）把目录注入会话。

    目录注入成一条 system 消息：system 分区在 M07 里 priority=0（永不被压），
    而目录只有 ~200 token——常驻成本可接受，换来的"模型随时知道有什么技能"
    （§7 问答 8：把模式匹配外包给理解能力的持有者）。
    """
    loader = loader or SkillLoader(roots=roots)
    if not loader.skills:
        loader.scan()
    register_skill_tools(registry, loader)
    if session is not None and loader.skills:
        from agent_godot.core import Message
        session.append(Message(role="system",
                               content=loader.catalog_prompt()))
    return loader


__all__ = ["CATALOG_TAG", "SkillSearchTool", "SkillUseTool",
           "register_skill_tools", "install_skills"]
