"""lab/m02/embed_test.py —— 真嵌入验收：ollama bge-m3 接入测试

验证三件事：
1. ollama 服务在 11434 就绪且 bge-m3 已拉取
2. 返回 1024 维向量（bge-m3 规格正确）
3. 语义有效性：中英翻译对应显著高于无关句 —— M01 mini_embed 实验
   里"随机向量 ≈ 0"的翻版，此刻换成真模型应看到 0.6+
"""

import httpx

BASE = "http://127.0.0.1:11434/v1"

# ① 服务就绪检查
models = httpx.get(f"{BASE}/models", timeout=10).json()
print("已安装模型:", [m["id"] for m in models["data"]])

# ② 编码三个句子：中英互译对 + 无关干扰句
texts = ["如何给敌人加碰撞伤害", "enemy collision damage", "今天天气怎么样"]
r = httpx.post(f"{BASE}/embeddings",
               json={"model": "bge-m3", "input": texts}, timeout=60)
vecs = [d["embedding"] for d in r.json()["data"]]
print("维度:", len(vecs[0]))                       # 期望 1024


def cos(a: list[float], b: list[float]) -> float:  # ollama 已归一化，点积即余弦
    return sum(x * y for x, y in zip(a, b))


# ③ 语义有效性：翻译对 vs 无关句
print("中英翻译对 cos:", round(cos(vecs[0], vecs[1]), 4))   # 期望 0.6+（真嵌入）
print("无关句     cos:", round(cos(vecs[0], vecs[2]), 4))   # 期望明显更低
