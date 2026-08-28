"""rag.rerank 单元测试：Embedding 轻量复排与 CrossEncoder 占位。"""

from __future__ import annotations

import pytest

from rag.embeddings import FakeEmbedding
from rag.rerank import CrossEncoderReranker, EmbeddingReranker
from rag.retriever import RetrievalResult


def _candidate(chunk_id: str, text: str) -> RetrievalResult:
    return RetrievalResult(chunk_id=chunk_id, source="a.md", score=0.1, text=text)


def test_rerank_orders_most_similar_first() -> None:
    """正常：与 query 相同的候选应排最前（余弦=1.0）。"""
    query = "红豆生南国 春来发几枝"
    candidates = [
        _candidate("far", "今天天气很好适合出门散步"),
        _candidate("same", query),
    ]
    reranker = EmbeddingReranker(FakeEmbedding(dim=64))
    results = reranker.rerank(query, candidates, top_k=2)
    assert len(results) == 2
    assert results[0].chunk_id == "same"
    assert results[0].score == pytest.approx(1.0)


def test_rerank_topk_truncates_and_updates_scores() -> None:
    """正常：top_k 截断返回数量，分数为重新计算的余弦。"""
    query = "向量数据库"
    candidates = [_candidate(f"c{i}", f"候选文本 {i}") for i in range(3)]
    reranker = EmbeddingReranker(FakeEmbedding(dim=64))
    results = reranker.rerank(query, candidates, top_k=1)
    assert len(results) == 1


def test_rerank_empty_and_invalid_topk() -> None:
    """边界/异常：空候选返回空列表；top_k<=0 抛 ValueError。"""
    reranker = EmbeddingReranker(FakeEmbedding(dim=64))
    assert reranker.rerank("查询", [], top_k=3) == []
    with pytest.raises(ValueError, match="top_k must be > 0"):
        reranker.rerank("查询", [_candidate("a", "文本")], top_k=0)


def test_cross_encoder_reranker_not_implemented() -> None:
    """占位：CrossEncoderReranker.rerank 抛 NotImplementedError。"""
    reranker = CrossEncoderReranker()
    with pytest.raises(NotImplementedError):
        reranker.rerank("查询", [_candidate("a", "文本")], top_k=1)
