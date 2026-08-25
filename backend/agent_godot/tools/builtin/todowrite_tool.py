"""tools/builtin/todowrite_tool.py —— 任务清单工具（M04 §4 步骤 7）

维护任务清单状态（pending/in_progress/done），多步工作的进度板。
这是 M13 plan 模式（DAG 计划执行）的雏形——先用最简单的线性清单。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..registry import BaseTool, register_tool
from ..response import ToolResponse

_MARK = {"pending": " ", "in_progress": "→", "done": "x"}


@register_tool(name="todo_write", readonly=False, risk="low", tags={"plan"})
class TodoWriteTool(BaseTool):
    """维护任务清单：整体写入当前任务列表与状态。规划多步工作时先建清单，完成一项更新一项。"""
    class Params(BaseModel):
        todos: list[dict] = Field(
            description="任务列表，每项 {content: str, status: pending|in_progress|done}")

    def __init__(self) -> None:
        self._todos: list[dict] = []

    async def run(self, todos: list[dict]) -> ToolResponse:
        # 规范化状态字段（模型可能给任意字符串）
        self._todos = [{"content": t.get("content", ""),
                        "status": t.get("status", "pending")
                        if t.get("status") in _MARK else "pending"}
                       for t in todos]
        lines = [f"{_MARK[t['status']]} {i}. {t['content']}"
                 for i, t in enumerate(self._todos, 1)]
        done = sum(1 for t in self._todos if t["status"] == "done")
        summary = "\n".join(lines) or "(空清单)"
        summary += f"\n（完成 {done}/{len(self._todos)}）"
        return ToolResponse(ok=True, summary=summary,
                            data={"todos": self._todos, "done": done})
