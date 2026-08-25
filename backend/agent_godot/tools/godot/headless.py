"""tools/godot/headless.py —— Godot 命令行执行器（M06 §1.5 / §4 步骤 4）

四级体检（对应工具三档超时，Dispatcher 的 headless tag）：
- L1 语法 --check-only（秒级，拦 GDScript 解析错）
- L2 导入 --import（秒~分钟，拦资源断链；新建资源后必须先跑，否则缓存里没有报假错）
- L3 测试 -s tests/run.gd（分钟，拦逻辑回归）
- L4 运行 场景跑 N 帧（分钟+，拦运行时崩溃；--quit-after 上限 + kill 兜底缺一不可）

错误解析器把 Godot 输出翻译成模型友好的 Observation（错误行号回填驱动自修复，
Reflection/客观验证器回路在领域层的第一次落地）。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Godot 错误行形如：res://bad.gd:5 - Parse Error: Unexpected token.
# 要求消息里含 error（忽略大小写）——防止 "at: res://x.gd:15" 之类的非错误引用被误报
GD_ERROR = re.compile(
    r"^(?P<file>res://[^\s:]+):(?P<line>\d+)(?:-(?P<end>\d+))?"
    r"\s*[-·]?\s*(?P<msg>.*(?:error|Error).*)$",
    re.MULTILINE)


@dataclass
class CheckResult:
    """一次 headless 校验的结果。"""
    ok: bool
    errors: list[dict] = field(default_factory=list)   # [{file,line,msg}]
    output: str = ""
    returncode: int | None = None


@dataclass
class RunResult(CheckResult):
    """L4 运行校验的结果（多了崩溃/超时标记）。"""
    crashed: bool = False


def find_godot() -> str | None:
    """定位 Godot 可执行文件：$GODOT_BIN 优先，然后 PATH 上的常见名字。"""
    env = os.environ.get("GODOT_BIN")
    if env and Path(env).exists():
        return env
    for cand in ("godot4", "godot", "godot4.exe", "godot.exe",
                 "Godot_v4.3-stable_win64.exe"):
        found = shutil.which(cand)
        if found:
            return found
    return None


class GodotRunner:
    """Godot 4 命令行执行器（headless）。"""

    def __init__(self, godot_bin: str | None, project_root: Path):
        self.godot_bin = godot_bin or find_godot()
        self.project_root = Path(project_root).resolve()

    @property
    def available(self) -> bool:
        """Godot 是否可用（不可用时工具层跳过校验并给出安装提示）。"""
        return bool(self.godot_bin)

    # ---------- 基础设施 ----------

    def _res(self, target: str) -> str:
        """文件系统相对路径 → res:// 路径（res:// 开头的原样返回）。"""
        t = target.replace("\\", "/")
        return t if t.startswith("res://") else "res://" + t.lstrip("/")

    async def _exec(self, args: list[str], timeout: float) -> tuple[str, int | None, bool]:
        """起子进程并限时等待；超时 kill 兜底（游戏改了主循环时帧计数不推进）。"""
        proc = await asyncio.create_subprocess_exec(
            self.godot_bin, *args, cwd=str(self.project_root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        timed_out = False
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()                       # ★ kill 兜底（§1.5 易错点③）
            await proc.wait()
            timed_out = True
            out = b""
        # stdout 编码随系统 locale 变（GBK 陷阱）：统一 replace + 关键字英文匹配
        return out.decode("utf-8", errors="replace"), proc.returncode, timed_out

    @staticmethod
    def parse_errors(output: str) -> list[dict]:
        return [m.groupdict() for m in GD_ERROR.finditer(output)]

    @staticmethod
    def _fallback_error(output: str, rc: int | None) -> dict:
        tail = (output.strip() or "").splitlines()[-1] if output.strip() else ""
        return {"file": "", "line": "",
                "msg": tail[-300:] or f"Godot 退出码 {rc}（无错误详情输出）"}

    # ---------- 四级校验 ----------

    async def check(self, script: str | None = None,
                    timeout: float = 15.0) -> CheckResult:
        """L1 语法检查（秒级）。script 可选——只查单个 .gd；不传查整个项目。"""
        args = ["--headless", "--path", str(self.project_root), "--check-only"]
        if script:
            args += ["--script", self._res(script)]
        out, rc, timed_out = await self._exec(args, timeout)
        if timed_out:
            return CheckResult(False, [{"file": "", "line": "",
                                        "msg": f"check 超时（{timeout}s）被强杀"}], out)
        errors = self.parse_errors(out)
        if rc == 0 and not errors:
            return CheckResult(True, [], out, rc)
        return CheckResult(False, (errors or [self._fallback_error(out, rc)])[:20],
                           out, rc)

    async def import_assets(self, timeout: float = 120.0) -> CheckResult:
        """L2 资源导入（新建贴图/场景后必须先跑，否则后续校验报"资源不存在"假错）。"""
        args = ["--headless", "--path", str(self.project_root), "--import"]
        out, rc, timed_out = await self._exec(args, timeout)
        if timed_out:
            return CheckResult(False, [{"file": "", "line": "",
                                        "msg": f"import 超时（{timeout}s）被强杀"}], out)
        errors = self.parse_errors(out)
        return CheckResult(rc == 0 and not errors, errors[:20], out, rc)

    async def run_tests(self, timeout: float = 120.0) -> CheckResult:
        """L3 测试（-s tests/run.gd，gut 入口）。"""
        entry = self.project_root / "tests" / "run.gd"
        if not entry.exists():
            return CheckResult(False, [{"file": "", "line": "",
                                        "msg": "未找到 tests/run.gd（gut 测试入口）"}])
        args = ["--headless", "--path", str(self.project_root),
                "-s", "res://tests/run.gd"]
        out, rc, timed_out = await self._exec(args, timeout)
        if timed_out:
            return CheckResult(False, [{"file": "", "line": "",
                                        "msg": f"测试超时（{timeout}s）被强杀"}], out)
        errors = self.parse_errors(out)
        failed = re.search(r"(\d+)\s+fail", out, re.IGNORECASE)
        n_failed = int(failed.group(1)) if failed else 0
        passed = re.search(r"(\d+)\s+pass", out, re.IGNORECASE)
        n_passed = int(passed.group(1)) if passed else 0
        ok = rc == 0 and not errors and n_failed == 0
        if ok and n_passed:
            out += f"\nTotals: {n_passed} passed, {n_failed} failed"
        return CheckResult(ok, errors[:20], out, rc)

    async def run_scene(self, scene: str, frames: int = 180,
                        timeout: float = 120.0) -> RunResult:
        """L4 运行校验：场景跑 N 帧 + stdout 断言（拦运行时崩溃/空引用/死循环）。"""
        args = ["--headless", "--path", str(self.project_root),
                self._res(scene), "--quit-after", str(frames)]
        out, rc, timed_out = await self._exec(args, timeout)
        if timed_out:
            return RunResult(False, [{"file": "", "line": "",
                                      "msg": f"运行 {timeout}s 未退出（帧计数可能未推进），"
                                             f"已强制终止"}],
                             out, None, crashed=True)
        errors = self.parse_errors(out)
        crashed = rc not in (0, None) or "SCRIPT ERROR" in out
        return RunResult(not crashed and not errors, errors[:20], out, rc, crashed)
