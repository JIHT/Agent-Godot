import httpx, json

BASE = "http://127.0.0.1:1234/v1"
models = httpx.get(f"{BASE}/models", timeout=10).json()
model_id = models["data"][0]["id"]
print("使用模型:", model_id)

# 请求：填一张"点菜单"
resp = httpx.post(f"{BASE}/chat/completions",
                  headers={"Authorization": "Bearer lm-studio"},  # 本地不校验，占位即可
                  json={"model": model_id, "stream": True,
                        "stream_options": {"include_usage": True},  # ★ 要账单（见下文 usage）
                        "messages": [{"role": "user", "content": "用一句话解释 token"}]},
                  timeout=120)

# 响应：SSE 按行到达，每行是一个 data: 前缀的 JSON
# data: {"choices":[{"delta":{"content":"令"}}]}
# data: {"choices":[{"delta":{"content":"牌"}}]}
# data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}   ← 工具调用信号
# data: {"choices":[],"usage":{"prompt_tokens":52,"completion_tokens":180,"total_tokens":232}}  ← ★末帧账单
# data: [DONE]
for line in resp.iter_lines():
    print(line)
    if not line.startswith("data: "):  # 忽略空行/注释行
        continue
    payload = line[6:]  # 剥掉 "data: " 前缀
    if payload == "[DONE]":  # 流结束标记
        break
    chunk = json.loads(payload)
    if not chunk["choices"]:  # ★ usage 末帧 choices 为空，必须先判空！
        usage = chunk.get("usage")
        continue
    delta = chunk["choices"][0]["delta"]
