"""tools/godot/check_tools.py —— 校验域 FC 工具（M06 §4 步骤 5）

headless 四级校验的工具出口（L1/L3/L4；L2 --import 由工具层在需要时内部调用）：
- godot_check（L1，秒级，只读可并发）
- godot_run_tests（L3，分钟级）
- godot_run_scene（L4，分钟+，跑 N 帧 + kill 兜底）

全部带 "headless" tag → Dispatcher 给 120s 档超时（M03 DispatchConfig.headless_timeout）。
校验错误不是灾难而是 Observation——错误行号回填驱动模型自修复。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..registry import BaseTool
from ..response import ErrorKind, ToolError, ToolResponse
from .headless import CheckResult, GodotRunner
from .scene_tools import GodotContext, _godot_tool


def _unavailable(tool: str) -> ToolResponse:
    return ToolResponse(ok=False, error=ToolError(
        kind=ErrorKind.INTERNAL, tool=tool,
        message="未找到 Godot 可执行文件，无法执行 headless 校验",
        hint="安装 Godot 4.x 并设置 GODOT_BIN 环境变量指向可执行文件；"
             "在此之前只能靠 read 工具人工核对"))


def _render(result: CheckResult, level: str, hint: str) -> ToolResponse:
    if result.ok:
        return ToolResponse(ok=True,
                            summary=f"{level} 校验通过\n{result.output.strip()[-500:]}"
                            if result.output.strip() else f"{level} 校验通过")
    errs = "\n".join(f"- {e['file']}:{e['line']} {e['msg']}"
                     for e in result.errors[:8])
    return ToolResponse(ok=False, error=ToolError(
        kind=ErrorKind.VALIDATION, tool=f"godot_{level.lower()}",
        message=errs or result.output.strip()[-500:], hint=hint))


@_godot_tool("godot_check", readonly=True, tags={"godot", "headless"})
class GodotCheckTool(BaseTool):
    """L1 语法校验（秒级）：GDScript 解析错误带行号回传。新建场景/资源后建议先跑一次。"""
    class Params(BaseModel):
        script: str = Field(default="",
                             description="可选，只校验单个脚本（如 'player.gd'）；缺省校验全项目")

    def __init__(self, ctx: GodotContext):
        self.ctx = ctx

    async def run(self, script: str = "") -> ToolResponse:
        if not self.ctx.runner.available:
            return _unavailable("godot_check")
        result = await self.ctx.runner.check(script or None)
        return _render(result, "L1 语法",
                       "按行号逐条修复；常见：缩进错误/未定义变量/信号签名不匹配。"
                       "新建资源后先 --import（godot_run_scene 前必跑）")


@_godot_tool("godot_run_tests", readonly=False, risk="medium",
             tags={"godot", "headless"})
class GodotRunTestsTool(BaseTool):
    """L3 测试校验（分钟级）：跑 tests/run.gd（gut 入口），解析通过/失败数。"""
    class Params(BaseModel):
        timeout: int = Field(default=120, ge=5, le=600,
                             description="超时秒数，默认 120")

    def __init__(self, ctx: GodotContext):
        self.ctx = ctx

    async def run(self, timeout: int = 120) -> ToolResponse:
        if not self.ctx.runner.available:
            return _unavailable("godot_run_tests")
        result = await self.ctx.runner.run_tests(timeout=timeout)
        if result.errors and result.errors[0]["msg"].startswith("未找到"):
            return ToolResponse(ok=False, error=ToolError(
                kind=ErrorKind.NOT_FOUND, tool="godot_run_tests",
                message=result.errors[0]["msg"],
                hint="创建 tests/run.gd 作为 gut 测试入口（SceneTree 脚本）"))
        return _render(result, "L3 测试",
                       "查看失败用例的断言输出，修复对应脚本后重跑")


@_godot_tool("godot_run_scene", readonly=False, risk="medium",
             tags={"godot", "headless"})
class GodotRunSceneTool(BaseTool):
    """L4 运行校验（分钟+）：headless 跑指定场景 N 帧，拦运行时崩溃/空引用/死循环。
    新建贴图/场景后第一次会自动先 --import（资源进缓存，否则报假错）。"""
    class Params(BaseModel):
        scene: str = Field(description="要运行的场景，如 'main.tscn'")
        frames: int = Field(default=180, ge=1, le=10000,
                            description="最多跑多少帧后自动退出（--quit-after）")
        import_first: bool = Field(default=True,
                                   description="运行前先 --import 新资源（默认开）")

    def __init__(self, ctx: GodotContext):
        self.ctx = ctx

    async def run(self, scene: str, frames: int = 180,
                  import_first: bool = True) -> ToolResponse:
        runner: GodotRunner = self.ctx.runner
        if not runner.available:
            return _unavailable("godot_run_scene")
        if import_first:
            imp = await runner.import_assets()
            if not imp.ok:
                return _render(imp, "L2 导入",
                               "资源导入失败：检查 .tscn/.tres 语法与资源路径是否断链")
        result = await runner.run_scene(scene, frames=frames)
        return _render(result, "L4 运行",
                       "运行期错误：检查空引用/信号连接目标是否存在/节点路径拼写。"
                       "若怀疑死循环，减小 frames 复现")
