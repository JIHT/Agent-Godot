"""agent_godot/cli.py —— godot-agent CLI（MI-1 阶段的应用端）

M03 验收入口：`godot-agent ask "问题"`。
用 argparse（标准库零依赖）实现最小可用命令；后续子命令增多可换 typer。

工作流：load_registry → 按 mode 取 LLM → 组装工具注册表 → Dispatcher/Loop
→ 起一个消费者并发打印事件流 → 跑 loop.run → 收尾。
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from agent_godot.agent import AgentLoop, Dispatcher, EventBus, Session
from agent_godot.core import load_registry
from agent_godot.tools import ToolRegistry
from agent_godot.tools.builtin import list_files, read_file


def build_tools() -> ToolRegistry:
    """组装内置工具注册表（M03 验收够用：两个只读工具）。"""
    reg = ToolRegistry()
    reg.register(name="list_files", description="列出指定目录下的文件与子目录",
                 parameters={"type": "object",
                             "properties": {"path": {"type": "string",
                                                     "description": "目录路径，默认当前目录"}},
                             "required": []}, readonly=True)(list_files)
    reg.register(name="read_file", description="读取文本文件内容（限 10 万字节）",
                 parameters={"type": "object",
                             "properties": {"path": {"type": "string",
                                                     "description": "文件路径"}},
                             "required": ["path"]}, readonly=True)(read_file)
    return reg


async def _render_events(bus: EventBus) -> None:
    """事件消费者：把 AgentEvent 渲染成终端输出。"""
    async for ev in bus.stream():
        t, p = ev.type, ev.payload
        if t == "text_delta":
            print(p.get("text", ""), end="", flush=True)      # 流式逐字
        elif t == "tool_call_start":
            print(f"\n[工具] {', '.join(p.get('calls', []))}")
        elif t == "tool_call_result":
            print(f"  ↳ {p.get('content', '')[:120]}")
        elif t == "loop_warning":
            print("\n[循环] 检测到重复调用，已提示模型换思路")
        elif t == "message_end":
            print()
            if p.get("truncated"):
                print(f"[收尾] 预算用尽（{p.get('stop_reason')}），以上为进展总结")
            u = p.get("usage", {})
            if u:
                print(f"[账单] 输入 {u.get('input', 0)} + 输出 {u.get('output', 0)}"
                      f" = {u.get('input', 0) + u.get('output', 0)} token")
        elif t == "error":
            print(f"\n[错误] {p.get('error', '')}")


async def run_ask(question: str, mode: str, model_ref: str | None) -> None:
    registry = load_registry()
    if model_ref:
        llm = registry.llm(model_ref)
        model_name = registry.get(model_ref).model
    else:
        llm = registry.llm_for_mode(mode)
        model_name = registry.get(registry.route(mode).ref).model

    dispatcher = Dispatcher(build_tools())
    bus = EventBus()
    loop = AgentLoop(llm, dispatcher, model=model_name, bus=bus)
    session = Session(session_id="cli-session")

    consumer = asyncio.create_task(_render_events(bus))
    result = await loop.run(session, question, mode=mode)
    await bus.close()
    await consumer
    print(f"\n[结束] stop_reason={result.stop_reason} steps={result.steps}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows GBK 终端：允许输出 ↳ 等符号
    parser = argparse.ArgumentParser(prog="godot-agent",
                                     description="Godot 游戏 Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="提问（ask 模式）")
    ask.add_argument("question", nargs="+", help="问题内容")
    ask.add_argument("--mode", default="ask", help="模式：ask/craft/plan/multi")
    ask.add_argument("--model", default=None, help="模型引用覆盖，如 lmstudio/auto")
    args = parser.parse_args()

    if args.command == "ask":
        asyncio.run(run_ask(" ".join(args.question), args.mode, args.model))


if __name__ == "__main__":
    main()
