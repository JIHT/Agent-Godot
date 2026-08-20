import numpy as np

rng = np.random.default_rng(42)
d_model, seq = 8, 4
x = rng.normal(size=(d_model, seq))
print("x的值:", x)

Wq, Wk, Wv = (rng.normal(size=(d_model, d_model)) for _ in range(3))
Q, K, V = x @ Wq, x @ Wk, x @ Wv


def causal_mask(n):  # 因果掩码：只能看过去
    return np.triu(np.ones((n, n)), k=1).astype(bool)


scores = Q @ K.T / np.sqrt(d_model)  # [seq, seq] 相似度矩阵
scores[causal_mask(seq)] = -1e9  # 未来位置屏蔽
attn = np.exp(scores) / np.exp(scores).sum(-1, keepdims=True)
out = attn @ V  # [seq, d_model]
