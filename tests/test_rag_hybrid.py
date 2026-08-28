"""rag.hybrid 单元测试：混合检索（向量+BM25 RRF）与轻量精排适配。"""

from __future__ import annotations

import pytest

from rag.bm25 import BigramBM25
from rag.embeddings import FakeEmbedding
from rag.hybrid import HybridRetriever, RerankRetriever
from rag.ingestion import Chunk
from rag.qdrant_store import QdrantConfig
from rag.rerank import EmbeddingReranker
from rag.retriever import QdrantRetriever


def _chunks() -> list[Chunk]:
    return [
        Chunk(
            chunk_id="c1",
            source="a.md",
            text="RAG 是检索增强生成。",
            metadata={"chunk_index": 0, "char_start": 0},
        ),
        Chunk(
            chunk_id="c2",
            source="b.md",
            text="向量数据库用于存储嵌入向量。",
            metadata={"chunk_index": 0, "char_start": 0},
        ),
        Chunk(
            chunk_id="c3",
            source="c.md",
            text="今天天气很好适合出门散步。",
            metadata={"chunk_index": 0, "char_start": 0},
        ),
    ]


def _make_cfg(collection: str) -> QdrantConfig:
    return QdrantConfig(mode="local", path=":memory:", collection_name=collection, vector_size=8)


def _make_hybrid(collection: str) -> HybridRetriever:
    emb = FakeEmbedding(dim=8)
    vector = QdrantRetriever(emb, _make_cfg(collection))
    hybrid = HybridRetriever(vector, BigramBM25(), top_candidates=30)
    hybrid.index(_chunks())
    return hybrid


def test_hybrid_dedups_same_chunk_across_paths() -> None:
    """正常：双路召回同一 chunk 时融合结果不重复。"""
    hybrid = _make_hybrid("hybrid_dedup")
    results = hybrid.search("RAG 检索增强", top_k=10)
    ids = [r.chunk_id for r in results]
    assert len(ids) == len(set(ids))


def test_hybrid_bm25_recalls_keyword_chunk() -> None:
    """正常：BM25 精确匹配「天气」的片段进入融合结果（稀疏路补位）。"""
    hybrid = _make_hybrid("hybrid_bm25")
    results = hybrid.search("天气很好", top_k=10)
    assert any(r.chunk_id == "c3" for r in results)


def test_hybrid_rejects_bad_topk() -> None:
    """异常：top_k<=0 抛 ValueError。"""
    hybrid = _make_hybrid("hybrid_bad")
    with pytest.raises(ValueError, match="top_k must be > 0"):
        hybrid.search("查询", top_k=0)


def test_rerank_retriever_returns_topk() -> None:
    """正常：RerankRetriever 粗召回后精排取 Top-K，且走 index 双路同步。"""
    emb = FakeEmbedding(dim=8)
    vector = QdrantRetriever(emb, _make_cfg("rr_wrap"))
    rr = RerankRetriever(vector, EmbeddingReranker(emb), coarse_top_k=20)
    rr.index(_chunks())
    results = rr.search("向量数据库", top_k=1)
    assert len(results) == 1
    assert results[0].chunk_id in {"c1", "c2", "c3"}
