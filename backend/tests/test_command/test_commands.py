"""tests/test_command/test_commands.py —— 命令三件套（M14 §1.2 / §5）

覆盖：解析、近邻提示、三类产出各一例、以及"命令是逃生舱"的两条性质——
① 不经过模型（handler 里没有 llm）② args 为空是追问不是报错。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_godot.agent import Session
from agent_godot.command import (Command, CommandContext, CommandParser,
                                 CommandRegistry, CommandResult)
from agent_godot.core import Message
from agent_godot.session.rewind import RewindReport


# ---------- 解析 ----------

def test_parse_command_and_args():
    p = CommandParser()
    assert p.parse("/plan 给 player.gd 加二段跳") == Command(
        name="plan", args="给 player.gd 加二段跳", raw="/plan 给 player.gd 加二段跳")
    assert p.parse("/skills").args == ""
    assert p.parse("普通问题") is None                    # 不是命令 → 走对话
    assert p.parse("https://x/y") is None                # 只有行首的 / 才算命令
    assert p.is_command("/compact") and not p.is_command("compact")


def test_command_sub_and_rest():
    c = Command(name="skills", args="use 打包发布")
    assert c.sub == "use" and c.rest == "打包发布"
    assert Command(name="skills", args="").sub == ""


# ---------- 分发与近邻提示 ----------

def _registry() -> CommandRegistry:
    return CommandRegistry().install_builtins()


async def test_unknown_command_suggests_neighbors():
    """§5：/plans → 提示 /plan（不报错，命令表面向人）。"""
    registry = _registry()
    result = await registry.dispatch("/plans 打包", CommandContext())
    assert result.kind == "direct"
    assert "/plan" in result.text
    assert "未知命令" in result.text
    assert registry.suggestions("plans") == ["plan"]
    assert registry.suggestions("zzzzzz") == []


async def test_non_command_string_returns_hint():
    result = await _registry().dispatch("普通一句话", CommandContext())
    assert result.kind == "direct" and "不是合法命令" in result.text


async def test_handler_exception_becomes_readable_message():
    """命令执行失败要给人话，不是栈（命令表面向人，异常不许冒泡到终端）。"""

    registry = CommandRegistry()

    async def boom(cmd: Command, ctx: CommandContext) -> CommandResult:
        raise RuntimeError("磁盘满了")

    registry.register("boom", boom, help="必炸命令")
    result = await registry.dispatch("/boom", CommandContext())
    assert result.kind == "direct"
    assert "执行失败" in result.text and "磁盘满了" in result.text


# ---------- 三类产出 ----------

async def test_help_is_direct_and_lists_commands():
    result = await _registry().dispatch("/help", CommandContext())
    assert result.kind == "direct"
    assert "/compact" in result.text and "/rewind" in result.text
    assert "/skills" in result.text and "/plan" in result.text
    assert "/model" in result.text and "/checkpoint" in result.text


async def test_plan_without_args_asks_back_not_error():
    """§1.2 易错点②：路由型命令缺参数 → 追问。"""
    result = await _registry().dispatch("/plan", CommandContext())
    assert result.kind == "direct"
    assert "要规划什么任务" in result.text


async def test_plan_with_args_is_prompt_inject_with_new_mode():
    ctx = CommandContext(mode="ask")
    result = await _registry().dispatch("/plan 打包并发布 Windows 版", ctx)
    assert result.kind == "prompt_inject"
    assert result.new_mode == "plan"                     # 命令只切模式，活还是模型干
    assert result.text == "打包并发布 Windows 版"


async def test_compact_is_state_change_and_shrinks_session():
    """/compact：确定性压缩（走 M07 Compressor，无 llm → B 档模板摘要）。"""
    session = Session(session_id="s1")
    for i in range(6):
        session.append(Message(role="user", content=f"问题 {i}"))
        session.append(Message(role="assistant", content=f"回答 {i}"))
    ctx = CommandContext(session=session)

    result = await _registry().dispatch("/compact", ctx)
    assert result.kind == "state_change"
    assert len(session.messages) == 1                    # 换成一条摘要
    assert session.messages[0].role == "system"
    assert "已压缩" in result.text


async def test_compact_rejects_short_session():
    session = Session(session_id="s1")
    session.append(Message(role="user", content="hi"))
    result = await _registry().dispatch("/compact",
                                        CommandContext(session=session))
    assert result.kind == "direct" and "还没到需要压缩" in result.text


async def test_compact_without_session_is_direct():
    result = await _registry().dispatch("/compact", CommandContext())
    assert result.kind == "direct" and "没有活动会话" in result.text


async def test_rewind_is_state_change_and_audited(tmp_path):
    """§1.2 易错点③：控制型命令也过审计（事件总线收到 rewind 记录）。"""
    calls: list = []

    @dataclass
    class FakeManager:
        project_root: object = tmp_path
        calls: list = field(default_factory=list)

        async def rewind(self, session_id: str, turns: int) -> RewindReport:
            calls.append((session_id, turns))
            return RewindReport(turns=turns, kept_turns=3, dropped_events=7,
                                files_restored=["a.gd"], task_ids=["t1"])

    events: list = []

    class FakeBus:
        async def emit(self, type_, **payload):
            events.append((type_, payload))

    loop = type("L", (), {"bus": FakeBus()})()
    ctx = CommandContext(session=Session(session_id="s-1"),
                         manager=FakeManager(), loop=loop)
    result = await _registry().dispatch("/rewind 2", ctx)
    assert result.kind == "state_change"
    assert calls == [("s-1", 2)]
    assert "已回退 2 轮" in result.text and "a.gd" in result.text
    assert any(t == "command" and p.get("cmd") == "rewind" for t, p in events)


async def test_rewind_argument_validation():
    result = await _registry().dispatch("/rewind abc", CommandContext())
    assert result.kind == "direct" and "轮数必须是正整数" in result.text


async def test_rewind_without_manager_is_direct():
    result = await _registry().dispatch("/rewind", CommandContext())
    assert result.kind == "direct" and "SessionManager" in result.text


async def test_model_shows_and_switches():
    switched: list[str] = []
    ctx = CommandContext(model="qwen/7b",
                         set_model=lambda ref: switched.append(ref))
    result = await _registry().dispatch("/model", ctx)
    assert result.kind == "direct" and "qwen/7b" in result.text

    result = await _registry().dispatch("/model gpt/4o", ctx)
    assert result.kind == "state_change"
    assert switched == ["gpt/4o"] and "gpt/4o" in result.text


async def test_model_switch_without_callback():
    result = await _registry().dispatch("/model gpt/4o", CommandContext())
    assert result.kind == "direct" and "未挂载模型切换回调" in result.text


async def test_checkpoint_list_without_saves(tmp_path):
    manager = type("M", (), {"project_root": tmp_path})()
    result = await _registry().dispatch("/checkpoint list",
                                        CommandContext(manager=manager))
    assert result.kind == "direct" and "没有命名存档" in result.text


async def test_checkpoint_requires_name(tmp_path):
    manager = type("M", (), {"project_root": tmp_path})()
    result = await _registry().dispatch("/checkpoint save",
                                        CommandContext(manager=manager))
    assert result.kind == "direct" and "需要存档名" in result.text


# ---------- /skills ----------

async def test_skills_list_direct_and_one_line_per_skill():
    from agent_godot.skills import SkillLoader

    loader = SkillLoader().scan()
    result = await _registry().dispatch("/skills", CommandContext(skills=loader))
    assert result.kind == "direct"
    assert result.data["count"] == len(loader.skills) >= 3
    for line in result.text.splitlines()[1:]:
        assert line.startswith("- ")                     # 每技能一行


async def test_skills_search_and_use():
    from agent_godot.skills import SkillLoader

    loader = SkillLoader().scan()
    ctx = CommandContext(skills=loader)
    hit = await _registry().dispatch("/skills search 打包发布 Windows 版", ctx)
    assert hit.kind == "direct" and "打包发布" in hit.text

    used = await _registry().dispatch("/skills use 打包发布", ctx)
    assert used.kind == "prompt_inject"
    assert "<skill name='打包发布'" in used.text
    assert "export_presets.cfg" in used.text             # 全文真的注入了


async def test_skills_use_unknown_suggests_neighbor():
    from agent_godot.skills import SkillLoader

    result = await _registry().dispatch(
        "/skills use 本地", CommandContext(skills=SkillLoader().scan()))
    assert result.kind == "direct" and "本地化" in result.text
