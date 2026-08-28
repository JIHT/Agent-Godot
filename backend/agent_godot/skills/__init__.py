"""skills：可插拔扩展三件套之「知识轴」（M14 §1.3）

领域方法论写成独立 SKILL.md（frontmatter + 正文），：**用到才全文注入**。
三步机制：①扫描建目录（常驻 ~200 token）②命中触发词或模型主动 skill_use
③全文注入本轮（进 trace 计 token）。

宿主用法：
    loader = install_skills(registry, session=session)   # 注册工具 + 注入目录
    body = await loader.load("打包发布")                  # 模型决定要用了才取全文

新增技能 = 在 `skills/builtin/<技能名>/SKILL.md`（或项目内
`.agent_godot/skills/<技能名>/SKILL.md`）放一个文件，零代码改动。
"""
from .loader import (SKILL_FILE, Skill, SkillLoader, default_roots,
                     parse_frontmatter)
from .registry import (SkillSearchTool, SkillUseTool, install_skills,
                       register_skill_tools)

__all__ = [
    "SKILL_FILE", "Skill", "SkillLoader", "default_roots", "parse_frontmatter",
    "SkillSearchTool", "SkillUseTool", "register_skill_tools", "install_skills",
]
