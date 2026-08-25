"""RAG 基础包。

包含文档导入（ingestion）、向量化（embeddings）、检索（retriever）、
生产型知识库编排（knowledge_rag / pdf_reader）与检索增强生成
（generator）等模块。
"""

from .embeddings import EmbeddingClient, FakeEmbedding, get_embedding
from .generator import (
    NO_ANSWER_TEXT,
    Citation,
    RagAnswer,
    RagGenerator,
    build_rag_messages,
)
from .ingestion import Chunk, build_chunks, chunk_text, load_documents
from .knowledge_rag import JwipcKnowledgeRAG, UnsupportedDocumentError
from .pdf_reader import parse_pdf
from .retriever import ListRetriever, RetrievalResult

__all__ = [
    "Chunk",
    "Citation",
    "EmbeddingClient",
    "FakeEmbedding",
    "JwipcKnowledgeRAG",
    "ListRetriever",
    "NO_ANSWER_TEXT",
    "RagAnswer",
    "RagGenerator",
    "RetrievalResult",
    "UnsupportedDocumentError",
    "build_chunks",
    "build_rag_messages",
    "chunk_text",
    "get_embedding",
    "load_documents",
    "parse_pdf",
]
