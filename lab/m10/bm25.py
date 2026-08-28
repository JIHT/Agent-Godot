"""lab/m10/bm25.py —— 手写 BM25 教学版（M10 §1.3 / §4 步骤 1）

BM25 = 词频饱和（k₁=1.5）+ 文档长度归一（b=0.75）+ 逆文档频率（IDF）。
直觉：本档出现越多分越高（饱和防刷词）、长文档稀释、全局罕见词权重大。

跑本文件：python lab/m10/bm25.py
观察点：
1. API 名查询（max_contacts_reported）——IDF 极高的稀有词主导排序：
   向量检索对这种"生僻 token"反而糊（嵌入空间里被拆成子词打散），BM25 精确抓
2. 教学版的已知局限：整句中文（无空格）会被 `\\w+` 当成一个词——
   工程版（agent_godot/rag/retrieval/bm25_index.py）用 jieba 解决
"""
import math
import re
from collections import Counter


class MiniBM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [re.findall(r"\w+", d.lower()) for d in docs]
        self.N = len(docs)
        self.avgdl = sum(map(len, self.docs)) / self.N
        self.tf = [Counter(d) for d in self.docs]
        self.df = Counter(w for d in self.docs for w in set(d))

    def idf(self, w):
        # ★ +1 平滑：查询词不在库中（df=0）不除零，仍得有限权（§1.3 易错点）
        return math.log((self.N - self.df[w] + 0.5) / (self.df[w] + 0.5) + 1)

    def search(self, query: str, top_k: int = 5):
        qs = re.findall(r"\w+", query.lower())
        scores = []
        for i, d in enumerate(self.docs):
            s = sum(self.idf(w) * (self.tf[i][w] * (self.k1 + 1)) /
                    (self.tf[i][w] + self.k1 * (1 - self.b + self.b * len(d) / self.avgdl))
                    for w in qs)
            scores.append((s, i))
        return sorted(scores, reverse=True)[:top_k]


if __name__ == "__main__":
    docs = [
        "CharacterBody2D 的 move_and_slide 返回值在 Godot 4.3 变为布尔，"
        "表示本帧是否发生碰撞，可用于落地检测。",
        "Area2D 的 body_entered 信号在 monitoring 与 monitorable 同时为真时触发。",
        "max_contacts_reported 属性控制每帧报告的最大碰撞数，"
        "Godot 4.3 起默认值从 4 提升到 8。",
        "GDScript 的 signal 声明可以带类型参数：signal hit(damage: int, by: Node)。",
        "信号连接推荐 connect(callable, CONNECT_DEFERRED) 避免物理帧内重入。",
    ]
    bm25 = MiniBM25(docs)

    for q in ["max_contacts_reported 默认值",        # API 名：稀有词 IDF 主导
              "碰撞 布尔",                            # 语义词：多处命中看 tf/长度
              "信号连接",                              # 中文词（有空格可切）
              "怎样让角色碰墙的时候不掉下去"]:          # ★ 教学版局限：整句一个词 → 全 0 分
        print(f"\n查询: {q!r}")
        for s, i in bm25.search(q, top_k=3):
            print(f"  {s:6.3f}  doc[{i}] {docs[i][:38]}")
