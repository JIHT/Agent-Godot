import numpy as np

rng = np.random.default_rng(0)

# 假装这是嵌入模型输出（真实场景用 sentence-transformers 加载 bge-small）
texts = ["如何给敌人加碰撞伤害", "enemy collision damage",
         "Godot 场景树结构", "recipe: apple pie", "GDScript 信号连接"]
emb = rng.normal(size=(len(texts), 16))
emb /= np.linalg.norm(emb, axis=1, keepdims=True)

def cos(a, b): return float(a @ b)

query = emb[0]
print([(texts[i], round(cos(query, emb[i]), 3)) for i in range(len(texts))])
# 随机向量下相似度 ≈ 0；换 bge 后"如何给敌人加碰撞伤害"与"enemy collision damage"
# 余弦应 > 0.7——跨语言语义对齐就是这么来的

# InfoNCE 手算（模拟一次训练 step）
tau = 0.07
q, k = rng.normal(size=(1, 8)), rng.normal(size=(4, 8))
q, k = q/np.linalg.norm(q), k/np.linalg.norm(k, axis=1, keepdims=True)
logits = (q @ k.T).squeeze() / tau          # [4] 相似度/温度
loss = -logits[0] + np.log(np.exp(logits).sum())
print("InfoNCE loss =", round(loss, 3))      # 随机时 ≈ log(4)=1.386，训练目标：把它压到 0