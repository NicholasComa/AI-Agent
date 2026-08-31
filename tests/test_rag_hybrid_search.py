"""rag.hybrid_search 单元测试：BM25 稀疏检索 + 混合检索（RRF）与精排适配。"""

from __future__ import annotations

import pytest

from rag.embeddings import FakeEmbedding
from rag.hybrid_search import BigramBM25, HybridRetriever, RerankRetriever, tokenize
from rag.ingestion import Chunk
from rag.qdrant_store import QdrantConfig
from rag.rerank import EmbeddingReranker
from rag.retriever import QdrantRetriever


def _bm25_chunks() -> list[Chunk]:
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
            text="今天天气很好。",
            metadata={"chunk_index": 0, "char_start": 0},
        ),
    ]


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


def test_tokenize_splits_ascii_words_and_cjk_bigrams() -> None:
    """正常：ASCII 按整词、中文按相邻汉字 bigram 切分。"""
    tokens = tokenize("RAG 检索增强")
    assert "rag" in tokens
    assert "检索" in tokens
    assert "索增" in tokens


def test_bm25_ranks_exact_token_match_first() -> None:
    """正常：与语料 token 精确匹配的片段排最前。"""
    bm25 = BigramBM25()
    bm25.index(_bm25_chunks())
    results = bm25.search("检索增强", top_k=1)
    assert results[0].chunk_id == "c1"


def test_bm25_index_dedups_by_chunk_id() -> None:
    """正常：重复 index 同一批 chunk 不会让语料条目翻倍。"""
    bm25 = BigramBM25()
    bm25.index(_bm25_chunks())
    bm25.index(_bm25_chunks())
    assert len(bm25) == 3
    results = bm25.search("检索增强", top_k=3)
    ids = [r.chunk_id for r in results]
    assert len(ids) == len(set(ids))


def test_bm25_raises_before_index_and_bad_topk() -> None:
    """异常：未建索引调用 search 抛 RuntimeError；top_k<=0 抛 ValueError。"""
    bm25 = BigramBM25()
    with pytest.raises(RuntimeError, match="has no index"):
        bm25.search("查询", top_k=1)
    bm25.index(_bm25_chunks())
    with pytest.raises(ValueError, match="top_k must be > 0"):
        bm25.search("查询", top_k=0)


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


def test_hybrid_auto_loads_bm25_from_vector_store() -> None:
    """正常：只写入向量库、BM25 为空时，首次 search 自动从向量库重建索引。"""
    emb = FakeEmbedding(dim=8)
    vector = QdrantRetriever(emb, _make_cfg("hybrid_autoload"))
    vector.index(_chunks())  # 等价于「上一次已导入，本次进程未重导」
    hybrid = HybridRetriever(vector, BigramBM25(), top_candidates=30)
    assert len(hybrid._bm25) == 0  # noqa: SLF001 - 直接断言内部索引状态

    results = hybrid.search("RAG 检索增强", top_k=3)

    assert results
    assert len(hybrid._bm25) == 3  # noqa: SLF001 - 已从向量库 scroll 重建


def test_hybrid_score_normalized_to_unit_range() -> None:
    """正常：融合结果的 score 落在 0~1，与 min_score 阈值口径一致。"""
    hybrid = _make_hybrid("hybrid_score_range")
    results = hybrid.search("RAG 检索增强", top_k=3)
    assert results
    for r in results:
        assert 0.0 <= r.score <= 1.0, f"score={r.score} 超出 0~1"


def test_hybrid_search_on_empty_collection_returns_empty() -> None:
    """边界：空集合下 BM25 无索引也不报错，退回纯向量返回空结果。"""
    emb = FakeEmbedding(dim=8)
    vector = QdrantRetriever(emb, _make_cfg("hybrid_empty"))
    hybrid = HybridRetriever(vector, BigramBM25())
    assert hybrid.search("任意查询", top_k=3) == []


def test_rerank_retriever_returns_topk() -> None:
    """正常：RerankRetriever 粗召回后精排取 Top-K，且走 index 双路同步。"""
    emb = FakeEmbedding(dim=8)
    vector = QdrantRetriever(emb, _make_cfg("rr_wrap"))
    rr = RerankRetriever(vector, EmbeddingReranker(emb), coarse_top_k=20)
    rr.index(_chunks())
    results = rr.search("向量数据库", top_k=1)
    assert len(results) == 1
    assert results[0].chunk_id in {"c1", "c2", "c3"}
