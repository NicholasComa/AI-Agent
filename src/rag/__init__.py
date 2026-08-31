"""RAG 基础包。

包含文档导入（ingestion）、向量化（embeddings）、检索（retriever）、
生产型知识库编排（knowledge_rag / pdf_reader）与检索增强生成
（generator）等模块；检索增强组件（metadata_filter / hybrid_search /
rerank）为可选能力，通过 ``build_retriever`` 策略工厂一键启用，默认不
改变既有纯向量检索行为。
"""

from .embeddings import EmbeddingClient, FakeEmbedding, get_embedding
from .generator import (
    NO_ANSWER_TEXT,
    Citation,
    RagAnswer,
    RagGenerator,
    build_rag_messages,
)
from .hybrid_search import BigramBM25, HybridRetriever, RerankRetriever, tokenize
from .ingestion import Chunk, build_chunks, chunk_text, load_documents
from .knowledge_rag import (
    RETRIEVAL_STRATEGIES,
    JwipcKnowledgeRAG,
    UnsupportedDocumentError,
    build_retriever,
)
from .metadata_filter import MetadataConditions, build_filter, file_type_is, source_is
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
    "RETRIEVAL_STRATEGIES",
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
    "build_retriever",
    "chunk_text",
    "file_type_is",
    "get_embedding",
    "load_documents",
    "parse_pdf",
    "source_is",
    "tokenize",
]
