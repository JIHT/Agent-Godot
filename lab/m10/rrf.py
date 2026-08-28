"""lab/m10/rrf.py —— RRF 融合实验（M10 §1.4 / §4 步骤 2）

两位评委（向量按意思、BM25 按术语）的分数量纲不同（余弦 0~1 vs BM25 0~25），
直接加权 = 拿身高加体重。RRF 只看名次：第 r 名得 1/(k+r)，两路共识者拿双票登顶。

跑本文件：python lab/m10/rrf.py
"""
from __future__ import annotations

from collections import defaultdict


def rrf_fuse(rankings: list[list[str]], k: int = 60, top_k: int = 10
             ) -> list[tuple[str, float]]:
    """rankings：每路一个有序 id 列表（rank 从 1 开始）。"""
    scores: dict[str, float] = {}
    for ranking in rankings:                       # 每路一个有序 id 列表
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])[:top_k]


if __name__ == "__main__":
    # ---- 手算对拍（§1.4 ③）：B、A 靠前（双票），E、C 各吃单边票 ----
    vec_rank = ["A", "B", "C", "D"]      # 余弦序
    bm25_rank = ["B", "E", "A", "F"]     # 词法序
    fused = rrf_fuse([vec_rank, bm25_rank])
    print("融合结果:", [(d, round(s, 5)) for d, s in fused])
    # 期望：B=1/62+1/61≈0.0324 > A=1/61+1/63≈0.0322 > E=1/62 ≈ C=1/63 > D=1/64=F=1/65

    # ---- 得票明细：共识如何登顶（可解释性是 RRF 的卖点）----
    k = 60
    votes: dict[str, list[str]] = defaultdict(list)
    for name, ranking in (("vec", vec_rank), ("bm25", bm25_rank)):
        for rank, d in enumerate(ranking, 1):
            votes[d].append(f"{name}:1/{k}+{rank}")
    for d, s in fused:
        print(f"  {d} = {s:.5f}  ← {' + '.join(votes[d])}")

    # ---- k 的旋钮效应：k 小头部支配（精英制），k 大名次差被抹平（平均制）----
    for k in (10, 60, 600):
        top = rrf_fuse([vec_rank, bm25_rank], k=k, top_k=2)
        gap = (top[0][1] - top[1][1]) / top[1][1]
        print(f"k={k:>4}: 榜首 {top[0][0]}（{top[0][1]:.5f}）"
              f"  次席 {top[1][0]}（{top[1][1]:.5f}）  头部差距 {gap:+.1%}")
