"""tools/godot/scene_tools.py —— 场景域 FC 工具（M06 §4 步骤 5）

领域工具 = 专业手术刀套装（对比通用文件工具的"菜刀"）：
- 模型认知减负：read_scene 返回结构化场景树，省 token 且不解析错
- 安全收口：结构化编辑在工具层拦截语法/引用错误，不让坏数据落盘
- 验收内建：写后自动触发 headless 校验，错误秒级回传自修复

项目管理 2 个 + 场景域 4 个，共 6 个。脚本域 3 个在 script_tools.py，
校验域 3 个在 check_tools.py——12 个核心工具由 register_godot_tools 统一注册。

所有路径相对 Godot 项目根（= 沙箱根）；节点路径相对场景根（不含根名）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from ..file_lock import OptimisticFileStore
from ..registry import BaseTool, ToolMeta
from ..response import Artifact, ErrorKind, ToolError, ToolResponse
from ..sandbox import (DENY_PARTS, DeniedPathError, PathEscapeError,
                       resolve_in_root)
from .checkpoints import TaskCheckpoints
from .headless import GodotRunner
from .scenes import SceneFile, SceneFormatError, SceneNode, parse_tscn


@dataclass
class GodotContext:
    """Godot 工具集的共享上下文（一个项目一份，全部工具复用同一实例）。"""
    project_root: Path
    store: OptimisticFileStore                          # 乐观锁读写（脚本域复用）
    runner: GodotRunner                                 # headless 校验
    checkpoints: TaskCheckpoints                        # 写前快照
    auto_check: bool = True


def build_godot_context(project_root: Path,
                        godot_bin: str | None = None) -> GodotContext:
    root = Path(project_root).resolve()
    return GodotContext(
        project_root=root,
        store=OptimisticFileStore(root),
        runner=GodotRunner(godot_bin, root),
        checkpoints=TaskCheckpoints(root))


def _godot_tool(name: str, *, readonly: bool = True,
                risk: str = "low", tags: set[str] | None = None):
    """挂 ToolMeta 但**不进全局注册表**——这些工具需要显式注入 GodotContext，
    不能被 from_global() 无参实例化（与 builtin 六件套的 store 注入同理）。"""
    def deco(cls):
        cls.meta = ToolMeta(name=name, description=(cls.__doc__ or "").strip(),
                            readonly=readonly, risk=risk, tags=tags or {"godot"})
        return cls
    return deco


def _denied(tool: str, e: Exception) -> ToolResponse:
    return ToolResponse(ok=False, error=ToolError(
        kind=ErrorKind.DENIED, tool=tool, message=str(e),
        hint="路径越出 Godot 项目根，请用项目内相对路径"))


def _resolve(ctx: GodotContext, rel: str) -> Path:
    return resolve_in_root(ctx.project_root, rel)


def _iter_project_files(ctx: GodotContext, suffix: str):
    for p in sorted(ctx.project_root.rglob(f"*{suffix}")):
        if any(x in DENY_PARTS for x in p.parts):
            continue
        yield p


async def _auto_check(ctx: GodotContext, script: str | None = None
                      ) -> tuple[bool, str]:
    """写后自动校验（L1）。返回 (是否通过, 附注文本)；无 Godot 时明确说跳过。"""
    if not ctx.runner.available:
        return True, "校验: 已跳过（未找到 Godot 可执行文件，可设 GODOT_BIN）"
    result = await ctx.runner.check(script)
    if result.ok:
        return True, "校验: godot_check 通过（L1 语法）"
    errs = "\n".join(f"- {e['file']}:{e['line']} {e['msg']}"
                     for e in result.errors[:8])
    return False, f"校验: 未通过（L1 语法）\n{errs}"


# ---------- 项目管理 ----------

def _project_settings(ctx: GodotContext) -> dict:
    """解析 project.godot 的 [application] 段（名称/主场景）。"""
    settings: dict[str, str] = {}
    gp = ctx.project_root / "project.godot"
    if not gp.exists():
        return settings
    in_app = False
    for line in gp.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("["):
            in_app = s == "[application]"
            continue
        if in_app and "=" in s:
            k, _, v = s.partition("=")
            settings[k.strip()] = v.strip().strip('"')
    return settings


@_godot_tool("godot_open_project", readonly=True)
class GodotOpenProjectTool(BaseTool):
    """校验并概览当前 Godot 项目（工具集绑定的项目根）。返回项目名、主场景与资源统计。"""
    class Params(BaseModel):
        pass

    def __init__(self, ctx: GodotContext):
        self.ctx = ctx

    async def run(self) -> ToolResponse:
        settings = _project_settings(self.ctx)
        if not settings and not (self.ctx.project_root / "project.godot").exists():
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.NOT_FOUND, tool="godot_open_project",
                message=f"{self.ctx.project_root} 下没有 project.godot，不是 Godot 项目",
                hint="用 --godot-root 指定正确的项目根，或先用 godot_create_scene 初始化"))
        scenes = sum(1 for _ in _iter_project_files(self.ctx, ".tscn"))
        scripts = sum(1 for _ in _iter_project_files(self.ctx, ".gd"))
        return ToolResponse(ok=True, summary=(
            f"项目就绪: {self.ctx.project_root.name}\n"
            f"项目名: {settings.get('config/name', '(未设置)')}\n"
            f"主场景: {settings.get('run/main_scene', '(未设置)')}\n"
            f"资源: {scenes} 个场景 / {scripts} 个脚本\n"
            f"下一步: godot_list_scenes 查看场景，godot_read_scene 读结构树"))


@_godot_tool("godot_project_overview", readonly=True)
class GodotProjectOverviewTool(BaseTool):
    """项目总览：全部场景/脚本清单与主场景，附 Godot 版本可用性（headless 校验是否开启）。"""
    class Params(BaseModel):
        pass

    def __init__(self, ctx: GodotContext):
        self.ctx = ctx

    async def run(self) -> ToolResponse:
        settings = _project_settings(self.ctx)
        scenes = [str(p.relative_to(self.ctx.project_root)).replace("\\", "/")
                  for p in _iter_project_files(self.ctx, ".tscn")]
        scripts = [str(p.relative_to(self.ctx.project_root)).replace("\\", "/")
                   for p in _iter_project_files(self.ctx, ".gd")]
        lines = [f"项目: {settings.get('config/name', self.ctx.project_root.name)}",
                 f"主场景: {settings.get('run/main_scene', '(未设置)')}",
                 f"场景({len(scenes)}): {', '.join(scenes) or '无'}",
                 f"脚本({len(scripts)}): {', '.join(scripts) or '无'}",
                 f"headless 校验: {'可用（' + self.ctx.runner.godot_bin + '）' if self.ctx.runner.available else '不可用（设 GODOT_BIN 开启）'}"]
        return ToolResponse(ok=True, summary="\n".join(lines))


# ---------- 场景域 ----------

@_godot_tool("godot_list_scenes", readonly=True)
class GodotListScenesTool(BaseTool):
    """列出项目全部 .tscn 场景（含每个场景的根节点与节点数）。"""
    class Params(BaseModel):
        pass

    def __init__(self, ctx: GodotContext):
        self.ctx = ctx

    async def run(self) -> ToolResponse:
        rows: list[str] = []
        for p in _iter_project_files(self.ctx, ".tscn"):
            rel = str(p.relative_to(self.ctx.project_root)).replace("\\", "/")
            try:
                sf = parse_tscn(p.read_text(encoding="utf-8", errors="replace"))
                root = sf.nodes[0] if sf.nodes else None
                rows.append(f"{rel}（{root.name} : {root.type or 'instance'}，"
                            f"{len(sf.nodes)} 节点）" if root else f"{rel}（空场景）")
            except (SceneFormatError, OSError, KeyError):
                rows.append(f"{rel}（解析失败，用 godot_read_scene 查看详情）")
        if not rows:
            return ToolResponse(ok=True, summary="项目里没有场景文件",
                                data={"scenes": []})
        return ToolResponse(ok=True, summary="\n".join(rows),
                            data={"scenes": len(rows)})


@_godot_tool("godot_read_scene", readonly=True)
class GodotReadSceneTool(BaseTool):
    """读取 .tscn 场景的结构化树（节点/类型/脚本/实例/信号连线），比裸读文本省 80% token。"""
    class Params(BaseModel):
        scene: str = Field(description="场景相对路径，如 'main.tscn' 或 'scenes/player.tscn'")

    def __init__(self, ctx: GodotContext):
        self.ctx = ctx

    async def run(self, scene: str) -> ToolResponse:
        try:
            p = _resolve(self.ctx, scene)
        except (PathEscapeError, DeniedPathError) as e:
            return _denied("godot_read_scene", e)
        if not p.exists():
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.NOT_FOUND, tool="godot_read_scene",
                message=f"场景不存在: {scene}",
                hint="先用 godot_list_scenes 查看可用场景"))
        try:
            sf = parse_tscn(p.read_text(encoding="utf-8", errors="replace"))
        except SceneFormatError as e:
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.VALIDATION, tool="godot_read_scene", message=str(e),
                hint="本项目只支持 Godot 4（format=3）的场景文件"))

        tree = sf.tree()
        lines = list(_render_tree(tree))
        if sf.connections:
            lines.append("")
            lines.append("信号连线:")
            for c in sf.connections:
                lines.append(f"- {c['signal']}: {c['from']} → {c['to']}"
                             f" (method={c['method']})")
        editable = ", ".join(sf._abs_path(n) for n in sf.nodes) or "（无节点）"
        lines.append("")
        lines.append(f"可编辑节点: {editable}")
        return ToolResponse(ok=True, summary="\n".join(lines),
                            data={"tree": tree, "scene": scene})


def _render_tree(node: dict, prefix: str = "") -> list[str]:
    """嵌套树 → 终端友好文本（├─/└─ 缩进）。"""
    lines: list[str] = []
    kids = node.get("children", [])
    for i, c in enumerate(kids):
        last = i == len(kids) - 1
        desc = c["name"]
        if c.get("type"):
            desc += f" ({c['type']})"
        if c.get("instance"):
            desc += f" [instance: {c['instance']}]"
        if c.get("script"):
            desc += f" [script: {c['script']}]"
        if c.get("opaque"):
            desc += " [opaque]"
        lines.append(prefix + ("└─ " if last else "├─ ") + desc)
        lines.extend(_render_tree(c, prefix + ("   " if last else "│  ")))
    return lines


@_godot_tool("godot_edit_scene", readonly=False, risk="medium")
class GodotEditSceneTool(BaseTool):
    """结构化编辑 .tscn 场景：ops 列表逐个应用（add_node/set_prop/connect_signal/remove_node）。
    写前自动快照（可回滚），写后自动触发 godot_check 校验。"""
    class Params(BaseModel):
        scene: str = Field(description="场景相对路径")
        ops: list[dict] = Field(description=(
            "编辑操作列表，逐个按序应用。可用 op：\n"
            '{"op":"add_node","parent":".","name":"Trap","type":"Area2D",'
            '"script":"res://trap.gd","instance":"res://other.tscn","props":{"key":"value"}}\n'
            '{"op":"set_prop","path":"Player","key":"position","value":"Vector2(120, 64)"}\n'
            '{"op":"connect_signal","signal":"body_entered","from":"Trap","to":".","method":"_on_trap"}\n'
            '{"op":"remove_node","path":"Trap"}\n'
            "节点路径相对场景根（不含根名）；script/instance 传 res:// 路径即可，资源 ID 自动解析"))
        auto_check: bool = Field(default=True,
                                 description="写后自动跑 L1 语法校验（默认开）")

    def __init__(self, ctx: GodotContext):
        self.ctx = ctx

    async def run(self, scene: str, ops: list[dict],
                  auto_check: bool = True) -> ToolResponse:
        try:
            p = _resolve(self.ctx, scene)
        except (PathEscapeError, DeniedPathError) as e:
            return _denied("godot_edit_scene", e)
        if not p.exists():
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.NOT_FOUND, tool="godot_edit_scene",
                message=f"场景不存在: {scene}",
                hint="新场景用 godot_create_scene 创建"))
        if not ops:
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.VALIDATION, tool="godot_edit_scene",
                message="ops 为空列表，无事可做"))
        original = p.read_text(encoding="utf-8", errors="replace")
        try:
            sf = parse_tscn(original)
        except SceneFormatError as e:
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.VALIDATION, tool="godot_edit_scene", message=str(e)))

        # 逐 op 应用——任一失败立即中止，坏数据不落盘（这就是"安全收口"）
        applied: list[str] = []
        for i, op in enumerate(ops):
            try:
                applied.append(f"{i + 1}. {_apply_op(sf, op)}")
            except (KeyError, ValueError) as e:
                return ToolResponse(ok=False, error=ToolError(
                    kind=ErrorKind.VALIDATION, tool="godot_edit_scene",
                    message=f"ops[{i}] 失败: {e}（已中止，文件未写入；"
                            f"此前成功的 op 也未落盘）",
                    hint="修正该 op 后整批重试"))

        new_text = sf.serialize()
        if new_text == original:
            return ToolResponse(ok=True,
                                summary="编辑完成但无实际变更\n" + "\n".join(applied))

        # ★ 先快照后写入（顺序铁律：反了没有回头路）
        self.ctx.checkpoints.snapshot(p, reason=f"edit_scene {scene}")
        p.write_text(new_text, encoding="utf-8")

        import difflib
        diff = "".join(difflib.unified_diff(
            original.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{scene}", tofile=f"b/{scene}"))

        summary = "编辑成功:\n" + "\n".join(applied) + "\n\n" + diff
        if auto_check and self.ctx.auto_check:
            ok, note = await _auto_check(self.ctx)
            summary += "\n" + note
            if not ok:
                return ToolResponse(
                    ok=False, summary=summary,
                    error=ToolError(
                        kind=ErrorKind.VALIDATION, tool="godot_edit_scene",
                        message="写入成功但校验未通过（详见上方 diff 与错误行号）",
                        hint="按行号修复：常见缩进错误/未定义变量/信号签名不匹配；"
                             "也可让 godot_write_script 修脚本后再试"),
                    artifacts=[Artifact(type="diff", ref=scene)])
        return ToolResponse(ok=True, summary=summary,
                            artifacts=[Artifact(type="diff", ref=scene)])


def _apply_op(sf: SceneFile, op: dict) -> str:
    """应用单个编辑 op，返回人话描述（校验失败抛 KeyError/ValueError）。"""
    if not isinstance(op, dict):
        raise ValueError(f"op 必须是对象，得到 {type(op).__name__}")
    kind = op.get("op")
    if kind == "add_node":
        for key in ("name",):
            if not op.get(key):
                raise ValueError(f"add_node 缺少必填字段 {key!r}")
        script_id = (sf.resource_for("Script", op["script"])
                     if op.get("script") else None)
        inst_id = (sf.resource_for("PackedScene", op["instance"])
                   if op.get("instance") else None)
        node = SceneNode(name=op["name"], type=op.get("type"),
                         parent=op.get("parent", "."),
                         instance_of=inst_id, script=script_id,
                         props={str(k): str(v) for k, v in
                                (op.get("props") or {}).items()})
        sf.add_node(op.get("parent", "."), node)
        where = op.get("parent", ".") or "."
        extra = " + script" if script_id else ""
        extra += " (instance)" if inst_id else ""
        return f"add_node {node.name} → {where}{extra}"
    if kind == "set_prop":
        for key in ("path", "key", "value"):
            if key not in op:
                raise ValueError(f"set_prop 缺少必填字段 {key!r}")
        sf.set_prop(op["path"], op["key"], str(op["value"]))
        return f"set_prop {op['path']}.{op['key']} = {op['value']}"
    if kind == "connect_signal":
        for key in ("signal", "from", "method"):
            if not op.get(key):
                raise ValueError(f"connect_signal 缺少必填字段 {key!r}")
        sf.connect_signal(op["signal"], op["from"],
                          op.get("to", "."), op["method"])
        return (f"connect_signal {op['signal']}: {op['from']} → "
                f"{op.get('to', '.')} :: {op['method']}")
    if kind == "remove_node":
        if not op.get("path"):
            raise ValueError("remove_node 缺少必填字段 'path'")
        count = sf.remove_node(op["path"])
        return f"remove_node {op['path']}（含子树共 {count} 个节点）"
    raise ValueError(f"未知 op 类型 {kind!r}"
                     f"（可用: add_node/set_prop/connect_signal/remove_node）")


@_godot_tool("godot_create_scene", readonly=False, risk="medium")
class GodotCreateSceneTool(BaseTool):
    """创建新的 .tscn 场景（根节点 + 可选脚本），自动生成 uid 并触发校验。"""
    class Params(BaseModel):
        path: str = Field(description="新场景相对路径，如 'scenes/trap.tscn'")
        root_name: str = Field(description="根节点名，如 'Trap'")
        root_type: str = Field(default="Node2D",
                               description="根节点类型，默认 Node2D")
        script: str = Field(default="",
                             description="可选，根节点脚本的 res:// 路径（如 res://trap.gd）")

    def __init__(self, ctx: GodotContext):
        self.ctx = ctx

    async def run(self, path: str, root_name: str, root_type: str = "Node2D",
                  script: str = "") -> ToolResponse:
        try:
            p = _resolve(self.ctx, path)
        except (PathEscapeError, DeniedPathError) as e:
            return _denied("godot_create_scene", e)
        if p.exists():
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.CONFLICT, tool="godot_create_scene",
                message=f"场景已存在: {path}",
                hint="改用 godot_edit_scene 编辑现有场景"))
        if not path.replace("\\", "/").endswith(".tscn"):
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.VALIDATION, tool="godot_create_scene",
                message="场景文件必须以 .tscn 结尾"))

        import secrets
        uid = "uid://" + "".join(secrets.choice(
            "abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(13))
        sf = SceneFile(header={"format": "3", "uid": uid})
        root = SceneNode(name=root_name, type=root_type)
        if script:
            res_path = (script if script.startswith("res://")
                        else "res://" + script.replace("\\", "/").lstrip("/"))
            root.script = sf.resource_for("Script", res_path)
        sf.add_node(".", root)

        # ★ 新文件也要快照（existed=False）——回滚=删除，否则留幽灵文件
        self.ctx.checkpoints.snapshot(p, reason=f"create_scene {path}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sf.serialize(), encoding="utf-8")

        summary = (f"已创建 {path}（根节点 {root_name} : {root_type}，"
                   f"uid={uid}）")
        if self.ctx.auto_check:
            ok, note = await _auto_check(self.ctx)
            summary += "\n" + note
        summary += "\n下一步: godot_edit_scene 添加子节点/属性/信号连线"
        return ToolResponse(ok=True, summary=summary,
                            artifacts=[Artifact(type="file", ref=path)])
