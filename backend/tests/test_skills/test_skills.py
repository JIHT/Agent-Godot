"""tests/test_skills/test_skills.py —— 知识外化与按需加载（M14 §1.3 / §5）

钉死三件事：①目录成本（常驻 <300 token）②按需加载（用到才进上下文）
③模型能自己检索（skill_search / skill_use 是工具，不是提示）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_godot.agent import Session
from agent_godot.command import CommandContext, CommandRegistry
from agent_godot.skills import (Skill, SkillLoader, SkillSearchTool,
                                SkillUseTool, install_skills,
                                parse_frontmatter, register_skill_tools)
from agent_godot.tools import ErrorKind, ToolRegistry

BUILTIN = (Path(__file__).resolve().parents[3] / "backend" / "agent_godot"
           / "skills" / "builtin")


def _loader() -> SkillLoader:
    """已扫描完的内置技能 loader（scan 按 §2 接口返回 list，不是 self）。"""
    loader = SkillLoader(roots=[BUILTIN])
    loader.scan()
    return loader


# ---------- frontmatter 解析 ----------

def test_parse_frontmatter_inline_lists():
    text = ("---\nname: 打包发布\ntriggers: [打包, 导出, export]\n"
            "tools_needed: [godot_check]\nversion: 2\n---\n# 标题\n正文\n")
    data, body = parse_frontmatter(text)
    assert data["name"] == "打包发布"
    assert data["triggers"] == ["打包", "导出", "export"]
    assert data["tools_needed"] == ["godot_check"]
    assert data["version"] == "2"
    assert body.strip().startswith("# 标题")


def test_parse_frontmatter_block_list_and_quotes():
    text = ('---\nname: "本地化"\ntriggers:\n  - 翻译\n  - i18n\nversion: 3\n---\n正文')
    data, _ = parse_frontmatter(text)
    assert data["name"] == "本地化"
    assert data["triggers"] == ["翻译", "i18n"]
    assert data["version"] == "3"


def test_parse_frontmatter_missing():
    data, body = parse_frontmatter("没有 frontmatter 的正文")
    assert data == {} and body == "没有 frontmatter 的正文"


def test_skill_from_markdown_defaults(tmp_path):
    """缺字段也能降级成可用技能（技能文档是数据，解析失败不该抛异常）。"""
    p = tmp_path / "裸技能" / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text("# 只有标题\n内容", encoding="utf-8")
    s = Skill.from_markdown(p)
    assert s.name == "裸技能"                            # 用目录名兜底
    assert s.version == 1
    assert s.description == "只有标题"                    # 用首个标题兜底
    assert s.triggers == []


# ---------- 扫描与目录 ----------

def test_scan_finds_builtin_skills():
    loader = _loader()
    names = loader.names()
    assert "打包发布" in names and "GDScript风格" in names and "本地化" in names
    pack = loader.by_name("打包发布")
    assert pack.version == 2
    assert "export" in pack.triggers
    assert "export_presets.cfg" in pack.body            # 正文真的读进来了


def test_catalog_prompt_is_one_line_per_skill_and_cheap():
    """> §5 验收：目录常驻成本 <300 token（20 个技能也不到 300）。"""
    loader = _loader()
    catalog = loader.catalog_prompt()
    lines = catalog.splitlines()
    assert len(lines) == len(loader.skills) + 1          # 一行引导 + 每技能一行
    assert loader.catalog_tokens() < 300


def test_higher_version_wins_on_duplicate_name(tmp_path):
    (tmp_path / "r1" / "打包发布").mkdir(parents=True)
    (tmp_path / "r1" / "打包发布" / "SKILL.md").write_text(
        "---\nname: 打包发布\nversion: 1\n---\n旧版", encoding="utf-8")
    (tmp_path / "r2" / "打包发布").mkdir(parents=True)
    (tmp_path / "r2" / "打包发布" / "SKILL.md").write_text(
        "---\nname: 打包发布\nversion: 5\n---\n新版", encoding="utf-8")
    loader = SkillLoader(roots=[tmp_path / "r1", tmp_path / "r2"])
    loader.scan()
    assert len(loader.skills) == 1
    assert loader.by_name("打包发布").body == "新版"


# ---------- 触发与加载 ----------

def test_match_by_trigger_is_case_insensitive():
    loader = _loader()
    assert [s.name for s in loader.match("帮我 export 一下")] == ["打包发布"]
    assert loader.match("今天天气不错") == []
    assert loader.match("") == []                        # 空输入不该命中全部


async def test_load_wraps_body_and_records_usage():
    loader = _loader()
    body = await loader.load("本地化")
    assert body.startswith("<skill name='本地化' version='1'>")
    assert body.endswith("</skill>")
    assert loader.loaded == ["本地化"]                    # 进 trace（上下文成本记账）
    assert loader.body_tokens("本地化") > 0


async def test_load_unknown_raises_with_neighbor():
    loader = _loader()
    with pytest.raises(KeyError) as exc:
        await loader.load("打包")
    assert "本地化" not in str(exc.value)                 # 近邻给的是打包发布
    assert "打包发布" in str(exc.value)


# ---------- 工具化（模型主动） ----------

def test_register_skill_tools():
    loader = _loader()
    reg = ToolRegistry()
    register_skill_tools(reg, loader)
    assert reg.has("skill_search") and reg.has("skill_use")
    assert reg.spec("skill_use").readonly is True        # 只读：不产生副作用


async def test_skill_search_returns_catalog_not_full_text():
    """目录 vs 全文：search 只回元数据行（省 token），use 才回全文。"""
    loader = _loader()
    tool = SkillSearchTool(loader)
    resp = await tool.execute('{"query": "帮我 export 一个 Windows 安装包"}')
    assert resp.ok
    assert "打包发布" in resp.summary
    assert "export_presets.cfg" not in resp.summary       # 全文没被带出来
    assert resp.data["names"] == ["打包发布"]


async def test_skill_use_returns_full_text_and_missing_is_not_found():
    loader = _loader()
    tool = SkillUseTool(loader)
    resp = await tool.execute('{"name": "打包发布"}')
    assert resp.ok and "export_presets.cfg" in resp.summary

    missing = await tool.execute('{"name": "没有这个技能"}')
    assert not missing.ok
    assert missing.error is not None
    assert missing.error.kind is ErrorKind.NOT_FOUND
    assert "skill_search" in (missing.error.hint or "")   # hint 指向下一步


async def test_install_skills_injects_catalog_into_session():
    loader = _loader()
    reg = ToolRegistry()
    session = Session(session_id="s1")
    install_skills(reg, loader, session=session)
    assert reg.has("skill_use")
    assert len(session.messages) == 1
    assert session.messages[0].role == "system"
    assert "打包发布" in session.messages[0].content       # 目录常驻上下文


# ---------- 与 /skills 命令协同（§1.4 三件套协同） ----------

async def test_command_skills_use_injects_into_next_turn():
    loader = _loader()
    registry = CommandRegistry().install_builtins()
    result = await registry.dispatch("/skills use 打包发布",
                                     CommandContext(skills=loader))
    assert result.kind == "prompt_inject"                # 回到循环，模型照手册干活
    assert "export_presets.cfg" in result.text
