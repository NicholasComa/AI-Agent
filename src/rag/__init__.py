"""RAG 基础包。

包含文档导入（ingestion）、向量化（embeddings）、检索（retriever）与生产型
知识库编排（knowledge_rag / pdf_reader）等模块。
"""

from .embeddings import EmbeddingClient, FakeEmbedding, get_embedding
from .ingestion import Chunk, build_chunks, chunk_text, load_documents
from .knowledge_rag import JwipcKnowledgeRAG, UnsupportedDocumentError
from .pdf_reader import parse_pdf
from .retriever import ListRetriever, RetrievalResult

__all__ = [
    "Chunk",
    "EmbeddingClient",
    "FakeEmbedding",
    "JwipcKnowledgeRAG",
    "ListRetriever",
    "RetrievalResult",
    "UnsupportedDocumentError",
    "build_chunks",
    "chunk_text",
    "get_embedding",
    "load_documents",
    "parse_pdf",
]
