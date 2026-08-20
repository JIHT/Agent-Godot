import numpy as np

logits = np.array([5.0, 4.0, 1.0, 0.5, -1, -2])  # 模型某一步的输出
vocab = ["the", "a", "cat", "dog", "zzz", "qqq"]


def softmax(z, T=1.0):
    z = z / T
    z = np.exp(z - z.max())  # 减 max 防溢出——必写，面试常问
    return z / z.sum()


def top_p_sample(probs, p=0.9):
    idx = np.argsort(-probs)  # 概率降序
    cum = np.cumsum(probs[idx])
    cut = np.searchsorted(cum, p) + 1  # 累计到 p 的最小集合
    keep = idx[:cut]
    renorm = probs[keep] / probs[keep].sum()
    return np.random.choice(keep, p=renorm)


for T in (0.1, 1.0, 2.0):
    print(T, np.round(softmax(logits, T), 3))
