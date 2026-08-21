"""lab/m03/react_trace.py —— 硬编码剧本看清 ReAct 循环骨架（M03 §1.1 ③ / §4 步骤 0）

不用真模型——先用三段剧本"看清机器，再通电"：
  第 1 轮 list_files → 第 2 轮 read_file → 第 3 轮输出总结
在 messages.append 处打印，亲眼看到上下文如何一轮轮长胖——
这就是 M07 上下文工程要治理的对象。
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")  # Windows GBK 终端：允许输出 ↳ 等 UTF-8 符号

# 模拟模型的三轮决策（索引 = 已发生的 tool 消息数）
SCRIPT = [
    {"tool_calls": [{"id": "call_1", "name": "list_files",
                     "arguments": '{"path": "../lab"}'}]},
    {"tool_calls": [{"id": "call_2", "name": "read_file",
                     "arguments": '{"path": "../lab/m02/embed_test.py"}'}]},
    {"content": "lab 下有几个实验脚本，embed_test.py 是 ollama bge-m3 的接入测试。"},
]

TOOLS = {
    "list_files": lambda **kw: os.listdir(kw.get("path", ".")),
    "read_file": lambda **kw: open(kw.get("path"), encoding="utf-8",
                                   errors="replace").read()[:500],
}


def fake_llm(messages):
    """按"已发生的工具消息数"取剧本（第 n 次工具调用后取第 n+1 段）。"""
    n_tools = sum(1 for m in messages if m["role"] == "tool")
    return SCRIPT[min(n_tools, len(SCRIPT) - 1)]


def react_loop(user_input: str, max_steps: int = 10) -> str:
    """ReAct 循环骨架：推理 → 调工具 → 观察回填 → 再推理。"""
    messages = [{"role": "user", "content": user_input}]
    for step in range(max_steps):
        resp = fake_llm(messages)
        print(f"--- step {step}: 模型返回 {list(resp.keys())} ---")
        if not resp.get("tool_calls"):
            print("→ 自然终止（Final Answer）")
            return resp["content"]
        messages.append({"role": "assistant", "tool_calls": resp["tool_calls"]})
        for tc in resp["tool_calls"]:
            result = TOOLS[tc["name"]](**json.loads(tc["arguments"]))
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": str(result)})
            print(f"  ↳ observation ({tc['name']}): {str(result)[:60]!r}")
    print("→ 预算耗尽")
    return "(预算耗尽) " + fake_llm(messages)["content"]


if __name__ == "__main__":
    answer = react_loop("看看 lab 下有什么，并讲讲 embed_test.py")
    print("\n最终回答:", answer)
