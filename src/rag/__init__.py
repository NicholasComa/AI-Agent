"""RAG 基础包。

包含文档导入（ingestion）、向量化（embeddings）与检索（retriever）三个模块。
"""

from .embeddings import EmbeddingClient, FakeEmbedding, get_embedding
from .ingestion import Chunk, build_chunks, chunk_text, load_documents
from .retriever import ListRetriever, RetrievalResult

__all__ = [
    "Chunk",
    "EmbeddingClient",
    "FakeEmbedding",
    "ListRetriever",
    "RetrievalResult",
    "build_chunks",
    "chunk_text",
    "get_embedding",
    "load_documents",
]
