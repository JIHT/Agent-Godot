"""rag：检索增强生成（M10）—— 给 Agent 配的开卷考场

闭卷（纯模型）靠背诵：会幻觉、会过时；开卷（RAG）先翻书再答题，
答案带页码（引用）。整条流水线是图书馆的数字化复刻：

- 离线建库：解析 parsers（采购编目）→ 切分 chunking（按主题上架）
  → 嵌入 embedding（bge-m3 统一度量衡）→ 双索引（Milvus 向量 + BM25 稀疏）
- 在线检索：混合检索 hybrid（馆员双路找书）→ RRF 融合（只看名次的计票规则）
  → 重排 rerank（馆长精挑）→ 引用 citation（带页码的答案）

数据源：Godot 官方文档库（预置公共库）+ 用户上传（PDF/URL/项目代码库）。
"""
from .citation import CITE_PROMPT, CitationFormatter
from .chunking import (Chunk, Chunker, RecursiveChunker, StructureAwareChunker,
                       breadcrumb, hard_split, recursive_split)
from .embedding import EmbeddingService, FakeEmbeddingService, QUERY_PREFIX
from .parsers import (GDScriptParser, HTMLParser, MarkdownParser, PDFParser,
                      ParsedDoc, Parser, ParserKind, URLParser, get_parser)
from .pipeline import ChunkStore, IngestPipeline
from .rerank import Reranker
from .retrieval import (BM25Index, HybridRetriever, InMemoryVectorIndex,
                       RetrievalHit, VectorIndex, rrf_fuse, tokenize)

__all__ = [
    # parsers
    "GDScriptParser", "HTMLParser", "MarkdownParser", "PDFParser", "ParsedDoc",
    "Parser", "ParserKind", "URLParser", "get_parser",
    # chunking
    "Chunk", "Chunker", "RecursiveChunker", "StructureAwareChunker",
    "breadcrumb", "hard_split", "recursive_split",
    # embedding
    "EmbeddingService", "FakeEmbeddingService", "QUERY_PREFIX",
    # retrieval
    "BM25Index", "HybridRetriever", "InMemoryVectorIndex", "RetrievalHit",
    "VectorIndex", "rrf_fuse", "tokenize",
    # rerank / citation
    "Reranker", "CITE_PROMPT", "CitationFormatter",
    # pipeline
    "ChunkStore", "IngestPipeline",
]
