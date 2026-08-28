"""agent_godot/cli.py —— godot-agent CLI（MI-1 阶段的应用端）

M03/M04 验收入口：`godot-agent ask "问题"`。
用 argparse（标准库零依赖）实现最小可用命令；后续子命令增多可换 typer。

工作流：load_registry → 按 mode 取 LLM → build_default_registry（M04 六件套）
→ Dispatcher/Loop → 消费者并发打印事件流 → loop.run → 收尾。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from agent_godot.agent import AgentLoop, Dispatcher, EventBus
from agent_godot.core import load_registry
from agent_godot.mcp.client import McpManager
from agent_godot.tools.builtin import build_default_registry


def _find_permissions(root: Path) -> Path | None:
    """找 permissions.yaml：项目根 / 项目根 config/ / cwd / cwd config/。"""
    for c in (root / "permissions.yaml", root / "config" / "permissions.yaml",
              Path.cwd() / "config" / "permissions.yaml",
              Path.cwd() / "permissions.yaml"):
        if c.exists():
            return c
    return None


async def _cli_prompter(pc):
    """CLI 确认门：谈话间出示手术方案（目标/参数/风险），家属签字。

    a = 本次会话不再问（remember: session，写入会话级授权集合）。
    """
    from agent_godot.permission.confirm import ConfirmAnswer

    def ask() -> str:
        print("\n[确认门] 需要你的批准")
        if pc.preview:
            print(f"  {pc.preview.replace(chr(10), chr(10) + '  ')}")
        else:
            print(f"  工具: {pc.tool}（风险: {pc.risk.value}）")
        while True:
            a = input("允许执行? [y=允许 / n=拒绝 / a=本次会话不再问] ").strip().lower()
            if a in ("y", "n", "a"):
                return a
            print("请输入 y / n / a")

    a = await asyncio.to_thread(ask)
    if a == "y":
        return ConfirmAnswer(approved=True)
    if a == "n":
        reason = await asyncio.to_thread(lambda: input("拒绝原因（可空）: ").strip())
        return ConfirmAnswer(approved=False, reason=reason or "用户拒绝执行")
    return ConfirmAnswer(approved=True, remember="session")


async def _cli_plan_approver(plan_text: str) -> bool:
    """CLI 计划审批（M13 plan 模式的任务级 HITL）：出示 DAG 等业主签字。"""

    def ask() -> bool:
        print("\n[计划审批]\n" + plan_text)
        while True:
            a = input("批准执行? [y=批准 / n=拒绝] ").strip().lower()
            if a in ("y", "n"):
                return a == "y"
            print("请输入 y / n")

    return await asyncio.to_thread(ask)


def _make_recorder(session):
    """dispatcher.on_result 钩子：单调用完成即 ToolDone 事件落盘（M09 §3）。"""
    from agent_godot.core import Message

    def record(call, resp) -> None:
        session.append(Message(role="tool", tool_call_id=call.id,
                               content=resp.render()))
    return record


async def _build_agent(question_mode: str, model_ref: str | None,
                       root: Path | None, godot_root: Path | None,
                       create_session: bool = True):
    """run_ask / run_resume 共用的组装：LLM + 工具 + 上下文 + 权限门 + 会话。"""
    from agent_godot.permission.confirm import ConfirmGate
    from agent_godot.permission.rules import RuleEngine
    from agent_godot.session import SessionManager

    registry = load_registry()
    if model_ref:
        llm = registry.llm(model_ref)
        model_name = registry.get(model_ref).model
    else:
        llm = registry.llm_for_mode(question_mode)
        model_name = registry.get(registry.route(question_mode).ref).model

    reg = build_default_registry(root)
    base_root = root or Path.cwd()
    project_root = godot_root or (
        base_root if (base_root / "project.godot").exists() else None)
    godot_ctx = None
    if project_root:
        from agent_godot.tools.godot import register_godot_tools
        godot_ctx = register_godot_tools(reg, project_root)
        print(f"[godot] 领域工具已接入（项目根: {project_root}）")

    # M09 权限门：permissions.yaml 找不到就用内置默认（low=allow/其余 ask）
    perm_path = _find_permissions(base_root)
    rules = RuleEngine(perm_path, project_root=str(base_root))
    if perm_path:
        print(f"[权限] 规则已加载: {perm_path}")

    manager = SessionManager(project_root or base_root)
    dispatcher = Dispatcher(reg)          # 先建 dispatcher，ConfirmGate 需要它
    session = await manager.create() if create_session else None
    gate = ConfirmGate(rules, session, dispatcher, registry=reg,
                       prompter=_cli_prompter)
    dispatcher.gate = gate                # 再挂门（gate 持有 dispatcher 引用）
    if session is not None:
        dispatcher.on_result = _make_recorder(session)   # ToolDone 即时落盘

    # M07 上下文工程：分区预算 + 保留配对滚动 + 三档压缩 + usage 自校准
    from agent_godot.context import (BudgetConfig, Compressor, ContextBuilder,
                                     HistoryManager, TokenCounter)
    counter = TokenCounter()
    context = ContextBuilder(
        counter=counter,
        compressor=Compressor(llm=llm, model=model_name),
        config=BudgetConfig(),
        history=HistoryManager(counter),
        model=model_name)
    loop = AgentLoop(llm, dispatcher, model=model_name, bus=None, context=context,
                     verify_runner=godot_ctx.runner if godot_ctx else None)
    return llm, loop, dispatcher, manager, session, rules


async def run_ask(question: str, mode: str, model_ref: str | None,
                  root: Path | None, godot_root: Path | None = None) -> None:
    llm, loop, dispatcher, manager, session, rules = await _build_agent(
        mode, model_ref, root, godot_root)
    loop.bus = EventBus()
    bus = loop.bus

    # MCP 服务器接入（mcp.yaml；单个失败不拖死 Agent，本地工具照常）
    mcp = McpManager(dispatcher.registry)
    await mcp.start_all()
    try:
        consumer = asyncio.create_task(_render_events(bus))
        if mode == "plan":
            # plan 模式走 DAG 外循环（生成计划 → 人审批 → 拓扑执行 → re-plan）
            from agent_godot.agent.paradigms import PlanStrategy
            strategy = PlanStrategy(llm=llm, loop=loop,
                                    approver=_cli_plan_approver)
            result = await strategy.run_plan_mode(session, question)
        else:
            result = await loop.run(session, question, mode=mode)
        await bus.close()
        await consumer
    finally:
        await mcp.stop_all()
        _save_grants(manager, session.session_id, rules)
    print(f"\n[结束] stop_reason={result.stop_reason} steps={result.steps} "
          f"session={session.session_id}")


async def run_resume(session_id: str | None, model_ref: str | None,
                     root: Path | None, godot_root: Path | None) -> None:
    """M09 /resume：断线/隔天恢复。waiting_confirm → 确认门续跑；否则续聊。"""
    from agent_godot.permission.confirm import resume_batch
    from agent_godot.session import SessionState

    llm, loop, dispatcher, manager, _, rules = await _build_agent(
        "ask", model_ref, root, godot_root, create_session=False)
    loop.bus = EventBus()
    bus = loop.bus

    session = await (manager.resume(session_id) if session_id
                     else manager.resume_latest())
    dispatcher.gate.session = session    # 确认门重绑到恢复的会话
    dispatcher.on_result = _make_recorder(session)
    print(f"[resume] 会话 {session.session_id} 状态: {session.state.value}"
          f"（{session.turns()} 轮）")
    # 恢复会话级授权记忆（"本次会话不再问"跨重启不丢）
    rules.restore_grants(_load_grants(manager, session.session_id))

    mcp = McpManager(dispatcher.registry)
    await mcp.start_all()
    try:
        consumer = asyncio.create_task(_render_events(bus))
        if session.state is SessionState.WAITING_CONFIRM:
            # 确认门续跑：签字 → 已完成响应表 → 从观察回填步继续（不重放副作用）
            answer = await _cli_prompter(session.pending_confirm)
            await manager.answer_confirm(session.session_id, answer,
                                         session=session)
            if answer.approved and answer.remember == "session":
                d = rules.decide(session.pending_confirm.tool,
                                 session.pending_confirm.args)
                if d.matched_rule:
                    rules.grant_session(d.matched_rule)
            done = await resume_batch(session, dispatcher)
            result = await loop.continue_with(session, done)
        else:
            # 续聊：纪要已在事件流里，直接以新输入驱动（L3 会话级恢复）
            question = await asyncio.to_thread(input, "[resume] 继续对话（输入内容）: ")
            result = await loop.run(session, question)
        await bus.close()
        await consumer
    finally:
        await mcp.stop_all()
        _save_grants(manager, session.session_id, rules)
    print(f"\n[结束] stop_reason={result.stop_reason} steps={result.steps}")


def _grants_path(manager, session_id: str) -> Path:
    return manager.project_root / ".agent_godot" / "grants" / f"{session_id}.json"


def _save_grants(manager, session_id: str, rules) -> None:
    """会话级授权指纹落盘（"本次会话不再问"跨重启不丢）。"""
    p = _grants_path(manager, session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rules.snapshot_for_resume()), encoding="utf-8")


def _load_grants(manager, session_id: str) -> list[str]:
    p = _grants_path(manager, session_id)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


async def _render_events(bus: EventBus) -> None:
    """事件消费者：把 AgentEvent 渲染成终端输出。"""
    async for ev in bus.stream():
        t, p = ev.type, ev.payload
        if t == "text_delta":
            print(p.get("text", ""), end="", flush=True)      # 流式逐字
        elif t == "tool_call_start":
            print(f"\n[工具] {', '.join(p.get('calls', []))}")
        elif t == "tool_call_result":
            print(f"  ↳ {p.get('content', '')[:150]}")
        elif t == "confirm_requested":
            print(f"\n[确认门] {p.get('tool', '')}（{p.get('risk', '')}）等待批准…")
        elif t == "loop_warning":
            print("\n[循环] 检测到重复调用，已提示模型换思路")
        elif t == "context_layout":
            layout = p.get("layout", {})
            total = sum(v for k, v in layout.items() if k != "total")
            detail = " ".join(f"{k}={v}" for k, v in layout.items())
            print(f"[上下文] 共 {total} token：{detail}")
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


async def run_rewind(index: int, root: Path | None,
                     turns: int | None = None, session_id: str | None = None) -> None:
    """/rewind 双粒度：

    - 默认（M06）：rewind 1 = 撤销最近一次任务的全部文件改动；
    - --turns N（M09）：丢掉最近 N 轮对话事件 + 联动回滚这些轮产生的
      全部文件检查点（对话-文件-记忆三联动，整个世界回到当时）。
    """
    from agent_godot.session import SessionManager
    if turns is not None:
        manager = SessionManager(root or Path.cwd())
        sid = session_id or (manager.store.session_ids()[-1]
                             if manager.store.session_ids() else None)
        if sid is None:
            print("没有任何会话记录")
            return
        report = await manager.rewind(sid, turns)
        print(f"已回退 {report.turns} 轮（保留 {report.kept_turns} 轮，"
              f"丢弃 {report.dropped_events} 个事件）")
        if report.task_ids:
            print(f"联动回滚任务检查点: {', '.join(report.task_ids)}")
        for f in report.files_restored:
            print(f"  - {f} 已恢复")
        return

    # M06 路径：任务级文件回滚
    from agent_godot.tools.godot import TaskCheckpoints
    ck = TaskCheckpoints(root or Path.cwd())
    infos = ck.list()
    if not infos:
        print("没有可回滚的任务检查点（.agent_godot/checkpoints 为空）")
        return
    if not 1 <= index <= len(infos):
        print(f"序号越界：可回滚 1..{len(infos)}（1 = 最近一次任务）")
        return
    info = infos[len(infos) - index]
    restored = ck.rollback(info.task_id)
    print(f"已回滚任务 {info.task_id}（{info.snapshots} 个快照，逆序回放）:")
    for f in restored:
        print(f"  - {f}")


async def run_checkpoint(action: str, name: str, root: Path | None) -> None:
    """/checkpoint save|restore|list —— 命名存档（打 Boss 前手动留退路）。"""
    from agent_godot.session import NamedCheckpoints, SessionManager
    manager = SessionManager(root or Path.cwd())
    nc = NamedCheckpoints(manager.project_root)
    if action == "save":
        task_ids = await manager.checkpoint_named("", name)
        print(f"已存档 {name!r}（聚合 {len(task_ids)} 个任务检查点）")
    elif action == "restore":
        files = nc.restore(name)
        print(f"已读档 {name!r}:")
        for f in files:
            print(f"  - {f}")
    else:
        for info in nc.list():
            print(f"  {info['name']}（{len(info.get('task_ids', []))} 个任务，"
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(info['ts']))}）")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows GBK 终端：允许输出 ↳ 等符号
    parser = argparse.ArgumentParser(prog="godot-agent",
                                     description="Godot 游戏 Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="提问（ask 模式）")
    ask.add_argument("question", nargs="+", help="问题内容")
    ask.add_argument("--mode", default="ask", help="模式：ask/craft/plan/multi")
    ask.add_argument("--model", default=None, help="模型引用覆盖，如 lmstudio/auto")
    ask.add_argument("--root", default=None,
                     help="工具沙箱的项目根目录（默认当前目录）")
    ask.add_argument("--godot-root", default=None,
                     help="Godot 项目根（默认自动检测当前目录的 project.godot）")

    craft = sub.add_parser("craft", help="执行模式（craft）：读-改-验-回滚闭环")
    craft.add_argument("question", nargs="+", help="任务内容")
    craft.add_argument("--model", default=None, help="模型引用覆盖")
    craft.add_argument("--root", default=None, help="工具沙箱的项目根目录")
    craft.add_argument("--godot-root", default=None, help="Godot 项目根")

    resume = sub.add_parser("resume", help="恢复会话（M09：确认门续跑 / 续聊）")
    resume.add_argument("session_id", nargs="?", default=None,
                        help="会话 ID（默认最近一个）")
    resume.add_argument("--model", default=None, help="模型引用覆盖")
    resume.add_argument("--root", default=None, help="工具沙箱的项目根目录")
    resume.add_argument("--godot-root", default=None, help="Godot 项目根")

    rewind = sub.add_parser("rewind", help="回滚（默认 M06 任务级；--turns 为 M09 轮级）")
    rewind.add_argument("index", nargs="?", type=int, default=1,
                        help="回滚第几个任务（1 = 最近一次，默认 1）")
    rewind.add_argument("--turns", type=int, default=None,
                        help="M09 模式：回退最近 N 轮对话（联动文件回滚）")
    rewind.add_argument("--session", default=None, help="M09 模式：目标会话 ID")
    rewind.add_argument("--root", default=None,
                        help="Godot 项目根（默认当前目录）")

    ckpt = sub.add_parser("checkpoint", help="命名存档（M09：save/restore/list）")
    ckpt.add_argument("action", choices=["save", "restore", "list"])
    ckpt.add_argument("name", nargs="?", default=None, help="存档名")
    ckpt.add_argument("--root", default=None, help="Godot 项目根（默认当前目录）")
    args = parser.parse_args()

    if args.command in ("ask", "craft"):
        mode = args.mode if args.command == "ask" else "craft"
        root = Path(args.root).resolve() if args.root else None
        godot_root = Path(args.godot_root).resolve() if args.godot_root else None
        asyncio.run(run_ask(" ".join(args.question), mode, args.model,
                            root, godot_root))
    elif args.command == "resume":
        root = Path(args.root).resolve() if args.root else None
        godot_root = Path(args.godot_root).resolve() if args.godot_root else None
        asyncio.run(run_resume(args.session_id, args.model, root, godot_root))
    elif args.command == "rewind":
        root = Path(args.root).resolve() if args.root else None
        asyncio.run(run_rewind(args.index, root, args.turns, args.session))
    elif args.command == "checkpoint":
        root = Path(args.root).resolve() if args.root else None
        if args.action != "list" and not args.name:
            print("save/restore 需要存档名")
            return
        asyncio.run(run_checkpoint(args.action, args.name or "", root))


if __name__ == "__main__":
    main()
