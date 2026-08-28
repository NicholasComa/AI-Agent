"""RAG 基础包。

包含文档导入（ingestion）、向量化（embeddings）、检索（retriever）、
生产型知识库编排（knowledge_rag / pdf_reader）与检索增强生成
（generator）等模块；检索增强组件（filters / bm25 / rerank / hybrid）
为可选能力，默认不改变既有纯向量检索行为。
"""

from .bm25 import BigramBM25, tokenize
from .embeddings import EmbeddingClient, FakeEmbedding, get_embedding
from .filters import MetadataConditions, build_filter, file_type_is, source_is
from .generator import (
    NO_ANSWER_TEXT,
    Citation,
    RagAnswer,
    RagGenerator,
    build_rag_messages,
)
from .hybrid import HybridRetriever, RerankRetriever
from .ingestion import Chunk, build_chunks, chunk_text, load_documents
from .knowledge_rag import JwipcKnowledgeRAG, UnsupportedDocumentError
from .pdf_reader import parse_pdf
from .rerank import CrossEncoderReranker, EmbeddingReranker, Reranker
from .retriever import ListRetriever, RetrievalResult

__all__ = [
    "BigramBM25",
    "Chunk",
    "Citation",
    "CrossEncoderReranker",
    "EmbeddingClient",
    "EmbeddingReranker",
    "FakeEmbedding",
    "HybridRetriever",
    "JwipcKnowledgeRAG",
    "ListRetriever",
    "MetadataConditions",
    "NO_ANSWER_TEXT",
    "RagAnswer",
    "RagGenerator",
    "RerankRetriever",
    "Reranker",
    "RetrievalResult",
    "UnsupportedDocumentError",
    "build_chunks",
    "build_filter",
    "build_rag_messages",
    "chunk_text",
    "file_type_is",
    "get_embedding",
    "load_documents",
    "parse_pdf",
    "source_is",
    "tokenize",
]
