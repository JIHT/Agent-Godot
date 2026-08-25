"""tools/godot：Godot 领域工具（M06）—— 场景/脚本/校验/检查点。

12 个核心 FC 工具（M06 §1.1 ③）：
- 项目管理: godot_open_project · godot_project_overview
- 场景域:   godot_list_scenes · godot_read_scene · godot_edit_scene · godot_create_scene
- 脚本域:   godot_read_script · godot_write_script（乐观锁）· godot_list_symbols
- 校验域:   godot_check（L1）· godot_run_tests（L3）· godot_run_scene（L4）

这些工具需要显式注入 GodotContext（项目根/Godot 可执行文件/检查点仓库），
因此不进全局注册表——用 register_godot_tools(registry, project_root) 显式注册。
"""
from __future__ import annotations

from pathlib import Path

from .checkpoints import CheckpointInfo, CheckpointStore, TaskCheckpoints
from .headless import CheckResult, GodotRunner, RunResult, find_godot
from .scenes import (SceneFile, SceneFormatError, SceneNode, parse_tscn,
                     parse_props)
from .scene_tools import (GodotContext, GodotCreateSceneTool,
                          GodotEditSceneTool, GodotListScenesTool,
                          GodotOpenProjectTool, GodotProjectOverviewTool,
                          GodotReadSceneTool, build_godot_context)


def register_godot_tools(registry, project_root: Path,
                         godot_bin: str | None = None) -> GodotContext:
    """把 12 个 Godot 工具注册进 registry，返回共享的 GodotContext。

    CLI 与 MCP 服务器（mcp/servers/godot）共用这个入口——同一实现双出口。
    """
    ctx = build_godot_context(project_root, godot_bin)
    from .check_tools import (GodotCheckTool, GodotRunSceneTool,
                              GodotRunTestsTool)
    from .script_tools import (GodotListSymbolsTool, GodotReadScriptTool,
                               GodotWriteScriptTool)
    for tool in (
            GodotOpenProjectTool(ctx), GodotProjectOverviewTool(ctx),
            GodotListScenesTool(ctx), GodotReadSceneTool(ctx),
            GodotEditSceneTool(ctx), GodotCreateSceneTool(ctx),
            GodotReadScriptTool(ctx), GodotWriteScriptTool(ctx),
            GodotListSymbolsTool(ctx),
            GodotCheckTool(ctx), GodotRunTestsTool(ctx), GodotRunSceneTool(ctx)):
        registry.register(tool)
    return ctx


__all__ = [
    "CheckpointInfo", "CheckpointStore", "TaskCheckpoints",
    "CheckResult", "RunResult", "GodotRunner", "find_godot",
    "SceneFile", "SceneFormatError", "SceneNode", "parse_tscn", "parse_props",
    "GodotContext", "build_godot_context", "register_godot_tools",
    "GodotOpenProjectTool", "GodotProjectOverviewTool",
    "GodotListScenesTool", "GodotReadSceneTool",
    "GodotEditSceneTool", "GodotCreateSceneTool",
]
