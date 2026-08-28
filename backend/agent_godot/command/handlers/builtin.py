"""command/handlers/builtin.py —— 内置斜杠命令（M14 §4 步骤 4）

命令的确定性（§7 问答 4）：/rewind 这类高危精确操作绝不能走"跟模型说
rewind"——模型可能理解错、可能反问、可能今天心情好给你 /plan 了。命令是
**模型故障时用户手里唯一的把手**（/compact /rewind /model = 逃生舱三件套）。

三类产出在这里各有一例：
- direct       ：/help /skills /model（查询）
- prompt_inject：/plan（路由：命令切模式，活还是模型干）
- state_change ：/compact /rewind /checkpoint（系统改完，通知模型世界变了）

★ 控制型命令也过审计（§1.2 易错点③）：/rewind 记录操作目标与轮数。
"""
from __future__ import annotations

from pathlib import Path

from agent_godot.core import Message

from ..parser import Command, CommandResult
from ..registry import CommandContext, register_command


# ---------- 查询型（direct） ----------

@register_command("help", help="列出全部斜杠命令；/help <名> 看用法",
                  usage="/help [命令名]")
async def cmd_help(cmd: Command, ctx: CommandContext) -> CommandResult:
    registry = ctx.registry
    if registry is None:
        return CommandResult.direct("命令表未挂载（宿主未注入 registry）")
    return CommandResult.direct(registry.help_text(cmd.args.strip()))


@register_command("skills", help="技能目录：list（默认）/ search <词> / use <名>",
                  usage="/skills [list|search 打包|use 打包发布]")
async def cmd_skills(cmd: Command, ctx: CommandContext) -> CommandResult:
    loader = ctx.skills or _default_loader()
    sub = cmd.sub
    rest = cmd.rest
    if sub in ("", "list", "ls"):
        catalog = loader.catalog_prompt()
        return CommandResult.direct(catalog or "（没有可用技能）",
                                    data={"count": len(loader.skills)})
    if sub in ("search", "find", "s"):
        hits = loader.match(rest) if rest else []
        if not hits:
            return CommandResult.direct(
                f"没有技能匹配 {rest!r}\n{loader.catalog_prompt()}")
        return CommandResult.direct("\n".join(
            f"- {s.name}（v{s.version}）: {s.description or '无描述'}"
            for s in hits))
    if sub in ("use", "load", "u"):
        if not rest:
            return CommandResult.direct(
                "要加载哪个技能？例：/skills use 打包发布\n"
                f"可用: {', '.join(loader.names())}")
        try:
            body = await loader.load(rest)
        except KeyError:
            from agent_godot.skills.loader import SkillLoader
            near = SkillLoader.nearest(loader.names(), rest)
            hint = f"（近邻: {near[0]}）" if near else ""
            return CommandResult.direct(
                f"没有技能 {rest!r}{hint}\n{loader.catalog_prompt()}")
        return CommandResult.prompt_inject(
            f"已加载技能《{rest}》，请严格按其中的步骤与检查清单执行：\n{body}")
    # 无子命令的一串文本 → 当搜索词（`/skills 打包` 也能用）
    return await cmd_skills(Command(name="skills", args=f"search {cmd.args}",
                                    raw=cmd.raw), ctx)


@register_command("model", help="查看 / 切换当前模型", usage="/model [模型引用]")
async def cmd_model(cmd: Command, ctx: CommandContext) -> CommandResult:
    ref = cmd.args.strip()
    if not ref:
        return CommandResult.direct(
            f"当前模型: {ctx.model or '(未设置)'}（当前模式: {ctx.mode}）")
    ok = await ctx.set_model_if_supported(ref)
    if not ok:
        return CommandResult.direct("当前环境未挂载模型切换回调，无法切换")
    return CommandResult.state_change(f"模型已切换为 {ref}", data={"model": ref})


# ---------- 路由型（prompt_inject） ----------

@register_command("plan", help="进入 plan 模式并规划任务（先出 DAG 再执行）",
                  usage="/plan 给 player.gd 加二段跳", kind="prompt_inject")
async def cmd_plan(cmd: Command, ctx: CommandContext) -> CommandResult:
    task = cmd.args.strip()
    if not task:
        # 路由型命令 args 为空 → 追问而不是报错（§1.2 易错点②）
        return CommandResult.direct(
            "要规划什么任务？例：/plan 给 player.gd 加二段跳\n"
            "（plan 模式会先生成 DAG 交给你审批，批准后才执行）")
    await ctx.emit("command", cmd="plan", task=task)
    return CommandResult.prompt_inject(task, new_mode="plan")


# ---------- 控制型（state_change） ----------

@register_command("compact", help="立即压缩上下文（不经过模型，零 token）",
                  usage="/compact", kind="state_change")
async def cmd_compact(cmd: Command, ctx: CommandContext) -> CommandResult:
    session = ctx.session
    if session is None:
        return CommandResult.direct("没有活动会话，无法压缩")
    msgs = list(getattr(session, "messages", None) or [])
    if len(msgs) < 4:
        return CommandResult.direct(
            f"当前仅 {len(msgs)} 条消息，还没到需要压缩的规模")
    before = _estimate(msgs)
    if hasattr(session, "compact_now"):
        out = session.compact_now()
        if hasattr(out, "__await__"):
            await out
    else:
        summary = await _summarize(ctx, msgs)
        _apply_compaction(session, summary)
    after = _estimate(list(getattr(session, "messages", None) or []))
    await ctx.emit("command", cmd="compact", before=before, after=after)
    return CommandResult.state_change(
        f"已压缩：{before} → {after} tokens（{len(msgs)} 条消息 → 1 条摘要）",
        data={"before": before, "after": after})


@register_command("rewind", help="回退最近 N 轮对话（联动回滚文件与记忆）",
                  usage="/rewind [轮数，默认 1]", kind="state_change")
async def cmd_rewind(cmd: Command, ctx: CommandContext) -> CommandResult:
    arg = cmd.args.strip()
    turns = 1
    if arg:
        if not arg.isdigit():
            return CommandResult.direct(
                f"轮数必须是正整数，如 /rewind 3（收到 {arg!r}）")
        turns = int(arg)
    if turns < 1:
        return CommandResult.direct("轮数必须 >= 1")
    manager = ctx.manager
    if manager is None:
        return CommandResult.direct(
            "当前环境未挂载 SessionManager，无法回退（会话事件仓库不可用）")
    session = ctx.session
    sid = getattr(session, "session_id", None)
    if sid is None:
        ids = manager.store.session_ids()
        if not ids:
            return CommandResult.direct("没有任何会话记录")
        sid = ids[-1]
    try:
        report = await manager.rewind(sid, turns)
    except (KeyError, ValueError) as e:
        return CommandResult.direct(f"回退失败: {e}")

    lines = [f"已回退 {report.turns} 轮（保留 {report.kept_turns} 轮，"
             f"丢弃 {report.dropped_events} 个事件）"]
    if report.task_ids:
        lines.append(f"联动回滚任务检查点: {', '.join(report.task_ids)}")
    for f in report.files_restored:
        lines.append(f"  - {f} 已恢复")
    text = "\n".join(lines)
    # 审计：高危命令必须留痕（操作了哪个会话、回退了几轮）
    await ctx.emit("command", cmd="rewind", turns=turns, session_id=sid,
                   files=report.files_restored)
    return CommandResult.state_change(text, data={"turns": turns,
                                                  "session_id": sid})


@register_command("checkpoint", help="命名存档：save / restore / list",
                  usage="/checkpoint save 重构前", kind="state_change")
async def cmd_checkpoint(cmd: Command, ctx: CommandContext) -> CommandResult:
    from agent_godot.session.rewind import NamedCheckpoints

    manager = ctx.manager
    base = Path(manager.project_root) if manager is not None else Path(
        ctx.project_root or Path.cwd())
    sub = cmd.sub or "list"
    name = cmd.rest
    nc = NamedCheckpoints(base)

    if sub in ("list", "ls"):
        items = nc.list()
        if not items:
            return CommandResult.direct("没有命名存档（用 /checkpoint save <名> 创建）")
        return CommandResult.direct("\n".join(
            f"  {i['name']}（{len(i.get('task_ids', []))} 个任务检查点）"
            for i in items))
    if not name:
        return CommandResult.direct(
            f"/checkpoint {sub} 需要存档名，例：/checkpoint {sub} 重构前")
    if manager is None:
        return CommandResult.direct("当前环境未挂载 SessionManager，无法存档")
    if sub == "save":
        task_ids = await manager.checkpoint_named("", name)
        await ctx.emit("command", cmd="checkpoint", action="save", name=name)
        return CommandResult.state_change(
            f"已存档 {name!r}（聚合 {len(task_ids)} 个任务检查点）")
    if sub == "restore":
        try:
            files = nc.restore(name)
        except KeyError as e:
            return CommandResult.direct(str(e))
        await ctx.emit("command", cmd="checkpoint", action="restore",
                       name=name, files=files)
        return CommandResult.state_change(
            f"已读档 {name!r}，恢复 {len(files)} 个文件"
            + ("".join(f"\n  - {f}" for f in files[:20]) if files else ""))
    return CommandResult.direct(f"未知子命令 {sub!r}（可用: save / restore / list）")


# ---------- 内部辅助 ----------

def _default_loader():
    """没有注入 loader 时扫内置技能（保证 /skills 永远有东西可列）。"""
    from agent_godot.skills.loader import SkillLoader
    return SkillLoader().scan()


def _estimate(msgs: list[Message]) -> int:
    try:
        from agent_godot.context.token_counter import TokenCounter
        return TokenCounter().estimate(msgs)
    except Exception:                                   # noqa: BLE001
        return sum(len(m.content or "") for m in msgs) // 4


async def _summarize(ctx: CommandContext, msgs: list[Message]) -> Message:
    """C 档（有 LLM）优先，无 LLM 自动降级 B 档模板摘要（M07）。"""
    compressor = ctx.compressor
    if compressor is None:
        from agent_godot.context.compressor import Compressor
        compressor = Compressor()                       # 无 llm → B 档兜底
    return await compressor.summarize(msgs, budget=800)


def _apply_compaction(session, summary: Message) -> None:
    """把压缩结果写回会话：事件溯源会话补记审计事件，普通会话直接换血。"""
    record = getattr(session, "record", None)
    if callable(record):
        try:
            from agent_godot.session.state import CompactDone, CompactStarted
            record(CompactStarted())
            record(CompactDone(summary=summary.content or ""))
        except Exception:                               # noqa: BLE001
            pass                                        # 审计事件记不上也要压缩成功
    try:
        session.messages[:] = [summary]
    except Exception:                                   # noqa: BLE001
        pass


__all__ = ["cmd_checkpoint", "cmd_compact", "cmd_help", "cmd_model",
           "cmd_plan", "cmd_rewind", "cmd_skills"]
