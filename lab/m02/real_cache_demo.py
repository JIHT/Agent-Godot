"""lab/m02/real_cache_demo.py —— 语义缓存真嵌入验收（M02 §1.5 的兑现时刻）

对照实验（同一组查询，两种嵌入）：
  ① 假嵌入：只认原句（字符串相同→向量相同），换说法永远 miss
  ② 真嵌入（ollama bge-m3）：换说法命中，无关问题不命中
  ③ 项目装配验证：models.yaml embedding 节 → registry 拿到的缓存是真嵌入

前置：ollama serve 在跑（ollama serve 或 Start-Process 隐藏启动）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from agent_godot.core.semantic_cache import OllamaEmbedder, SemanticCache  # noqa: E402

Q1 = [{"role": "user", "content": "怎么给敌人加碰撞伤害"}]
Q2 = [{"role": "user", "content": "敌人如何增加碰撞伤害"}]     # 同义改写
Q3 = [{"role": "user", "content": "怎么导出 Godot 游戏"}]      # 无关问题
ANSWER = "答案A：给敌人挂 Area2D，连接 body_entered 信号，在回调里扣血。"

# 预热：首次 embed 会触发 bge-m3 冷加载（1.2GB，可到几十秒），
# 等它就绪再计时实验——生产首次请求同理，冷启动成本一次性。
print("预热：等待 bge-m3 就绪（首次运行可能较慢）...")
warmer = OllamaEmbedder(timeout=120.0)
while warmer("warmup") is None:
    print("  还在加载，重试...")
    import time as _t; _t.sleep(2)
print(f"就绪（dim={warmer.dim}）\n")

print("=== ① 假嵌入（CI/单测形态，无语义能力）===")
fake = SemanticCache()
fake.put(Q1, ANSWER)
print("原句查询命中:  ", fake.get(Q1) is not None)     # True：同文本同哈希向量
print("换说法查询命中:", fake.get(Q2) is not None)     # False：随机方向 ≠ 语义
print("无关查询命中:  ", fake.get(Q3) is not None)

print()
print("=== ② 真嵌入（ollama bge-m3，1024 维）===")
real = SemanticCache(embedder=OllamaEmbedder(), threshold=0.92)
real.put(Q1, ANSWER)
print("嵌入维度:", real._locked_dim)
print("原句查询命中:  ", real.get(Q1) is not None)     # True
hit = real.get(Q2)
print("换说法查询命中:", hit is not None,             # ★ 假嵌入时代永远 False
      "→", (hit or "")[:24] + "…")
print("无关查询命中:  ", real.get(Q3) is not None)     # False：语义距离不够

print()
print("=== ③ 项目装配验证（models.yaml embedding 节）===")
from agent_godot.core import load_registry  # noqa: E402

registry = load_registry("../config/models.yaml")
embedder = registry._cache._embed
print("registry 装配的 embedder:", type(embedder).__name__)
if isinstance(embedder, OllamaEmbedder):
    v = embedder("装配自检")
    print("嵌入服务连通:", v is not None and len(v) == 1024)
