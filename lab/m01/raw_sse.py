import httpx, json

BASE = "http://127.0.0.1:1234/v1"

# 0. 先查服务与真实模型 ID（不要硬编码）
models = httpx.get(f"{BASE}/models", timeout=10).json()
model_id = models["data"][0]["id"]
print("使用模型:", model_id)

# 1. 流式请求（本地无需真实 key，占位即可）
resp = httpx.post(f"{BASE}/chat/completions",
    headers={"Authorization": "Bearer lm-studio"},
    json={"model": model_id, "stream": True,
          "messages": [{"role": "user", "content": "用一句话解释 token"}]},
    timeout=120)                     # 35B IQ3_XXS 半 CPU 半 GPU，首 token 可能慢，放宽

# 2. 逐行消费 SSE —— 同时消费思考与正文字段
for line in resp.iter_lines():
    if not line.startswith("data: "):
        continue
    payload = line[6:]
    if payload == "[DONE]":
        break
    delta = json.loads(payload)["choices"][0]["delta"]
    if delta.get("reasoning_content"):          # Qwen3 思考流（LM Studio 扩展字段）
        print(delta["reasoning_content"], end="", flush=True)
    if delta.get("content"):                    # 正文流
        print(delta["content"], end="", flush=True)
print("\n--- DONE ---")
