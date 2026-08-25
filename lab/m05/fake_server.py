"""lab/m05/fake_server.py —— 20 行级假 MCP 服务器（M05 §4 步骤 0，测试基石）

stdin 读 JSON-RPC → 按 method 分支 → stdout 写单行 JSON。
暴露 2 个工具：echo（回显）/ add（加法）。集成测试以子进程方式拉起它。

手工验证：echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python fake_server.py
"""
import json
import sys

TOOLS = [
    {"name": "echo", "description": "原样返回输入文本",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "add", "description": "两数相加",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "number"},
                                    "b": {"type": "number"}},
                     "required": ["a", "b"]}},
]


def reply(msg_id, result):
    print(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result},
                     ensure_ascii=False), flush=True)   # ★ 单行 + flush


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method", ""), msg.get("id")

    if method == "initialize":
        reply(mid, {"protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0.1"}})
    elif method == "notifications/initialized":
        pass                                          # 通知无响应
    elif method == "tools/list":
        reply(mid, {"tools": TOOLS})
    elif method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments", {})
        if name == "echo":
            reply(mid, {"content": [{"type": "text", "text": args.get("text", "")}]})
        elif name == "add":
            reply(mid, {"content": [{"type": "text",
                                     "text": str(args.get("a", 0) + args.get("b", 0))}]})
        else:
            reply(mid, {"content": [{"type": "text", "text": f"未知工具 {name}"}],
                        "isError": True})
