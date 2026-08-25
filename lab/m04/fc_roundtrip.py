"""lab/m04/fc_roundtrip.py —— FC 协议往返实验（M04 §1.1 ③ / §4 步骤 0）

亲眼见证"模型只有建议权"：
  ① 声明工具随请求发送 → ② 模型输出结构化建议（tool_calls）
  → ③ 本地执行真实函数 → ④ 结果作为 tool 消息回传 → ⑤ 模型给出自然语言回答

前置：LM Studio 或 ollama 跑着 LLM（craft 模式默认路由 lmstudio/auto）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from agent_godot.core import LLMRequest, Message, load_registry  # noqa: E402
from agent_godot.tools.builtin import build_default_registry  # noqa: E402


async def main() -> None:
    registry = load_registry()
    llm = registry.llm_for_mode("craft")                     # 本地模型
    model = registry.get(registry.route("craft").ref).model
    tools = build_default_registry(Path(__file__).resolve().parents[2])
    print(f"模型: {model} | 可用工具: {tools.names()}\n")

    messages = [Message(role="user", content="看看 lab 目录下有什么实验，简要说明。")]

    print("=== ① 第 1 轮请求（带工具声明）→ 模型的建议 ===")
    resp = await llm.complete(LLMRequest(
        model=model, messages=messages, tools=tools.tool_specs(),
        temperature=0.1))
    if resp.content:
        print(f"模型附言: {resp.content[:80]}")
    if not resp.tool_calls:
        print("（模型未调工具，直接回答——换更强的模型或更明确的任务试试）")
        return
    for tc in resp.tool_calls:
        print(f"→ 模型建议: {tc.name}({tc.arguments[:80]})")

    print("\n=== ② 本地执行（执行权在我们手里）===")
    messages.append(Message(role="assistant", tool_calls=resp.tool_calls))
    for tc in resp.tool_calls:
        result = await tools.get(tc.name).execute(tc.arguments)
        observation = result.render_for_model()
        print(f"← Observation: {observation[:120]}")
        messages.append(Message(role="tool", tool_call_id=tc.id,
                                content=observation))

    print("\n=== ③ 第 2 轮请求（回传结果）→ 自然语言回答 ===")
    resp2 = await llm.complete(LLMRequest(
        model=model, messages=messages, temperature=0.1))
    print(resp2.content)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
