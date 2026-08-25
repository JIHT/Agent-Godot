"""memory：分层记忆系统（M08）—— 抽取 · 召回 · 污染治理。

跨会话长期记忆：会话结束 → Extractor 复盘抽取 → Store 分类落库；
下次会话开始 → Retriever 按当前任务召回 → 注入 ContextBuilder memory 分区。

四层记忆（认知心理学分层，工程意义=不同遗忘策略）：
- 工作记忆：当前会话上下文（M07 消息列表，天然在场，不重复存 Store）
- 情景记忆：带时间地点的任务叙事（跨会话，τ=14 天指数衰减检索）
- 语义记忆：去时间化提炼事实/偏好/约定（长期稳定，τ=90 天）
- 项目画像：结构化档案（GDScript 版本/场景清单/依赖，直查不检索）

Mem0 式四路写入决策（ADD/UPDATE/DELETE/NOOP）在源头做减法——
每次写入后库里保持"最简且自洽"，避免只增不删的记忆库劣化。
"""
from .extractor import Decision, ExtractReport, MemoryExtractor
from .profile import ProfileEvent, ProfileManager, ProjectProfile
from .retriever import (MemoryRetriever, RecallConfig, ScoredMemory,
                        make_memory_provider)
from .store import (Embedder, MemoryRecord, MemoryStore, cosine, fake_embed)

__all__ = [
    # store
    "Embedder", "MemoryRecord", "MemoryStore", "cosine", "fake_embed",
    # retriever
    "MemoryRetriever", "RecallConfig", "ScoredMemory", "make_memory_provider",
    # extractor
    "Decision", "ExtractReport", "MemoryExtractor",
    # profile
    "ProfileEvent", "ProfileManager", "ProjectProfile",
]
