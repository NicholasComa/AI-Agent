"""retriever 与 embedding 单元测试。"""

from __future__ import annotations

import pytest

from rag.embeddings import FakeEmbedding
from rag.ingestion import Chunk
from rag.retriever import ListRetriever, RetrievalResult, _cosine


def _chunks() -> list[Chunk]:
    return [
        Chunk("a#0", "a.md", "向量数据库 Qdrant 用于相似度检索", {}),
        Chunk("b#0", "b.md", "今天天气很好适合出门散步", {}),
        Chunk("c#0", "c.md", "余弦相似度衡量两个向量的方向", {}),
    ]


def test_fake_embedding_is_deterministic_and_unit_norm():
    emb = FakeEmbedding(dim=64)
    v1 = emb.embed(["RAG 检索增强生成"])[0]
    v2 = emb.embed(["RAG 检索增强生成"])[0]
    assert v1 == v2, "相同文本应得到完全相同的向量"
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-9, "非空文本向量应为单位向量"


def test_fake_embedding_empty_text_is_zero_vector():
    emb = FakeEmbedding(dim=64)
    v = emb.embed([""])[0]
    assert all(x == 0.0 for x in v)


def test_search_returns_required_fields():
    retriever = ListRetriever(FakeEmbedding(dim=64))
    retriever.index(_chunks())
    results = retriever.search("向量相似度检索", top_k=3)
    assert results, "应返回结果"
    for r in results:
        assert isinstance(r, RetrievalResult)
        assert r.chunk_id and r.source and r.text
        assert isinstance(r.score, float)


def test_search_ranks_matching_chunk_first():
    retriever = ListRetriever(FakeEmbedding(dim=64))
    retriever.index(_chunks())
    top = retriever.search("Qdrant 向量数据库相似度检索", top_k=1)[0]
    assert top.chunk_id == "a#0", "与查询最相关的片段应排第一"


def test_search_respects_top_k_limit():
    retriever = ListRetriever(FakeEmbedding(dim=64))
    retriever.index(_chunks())
    assert len(retriever.search("任意查询", top_k=2)) == 2


def test_search_top_k_invalid_raises():
    retriever = ListRetriever(FakeEmbedding(dim=64))
    retriever.index(_chunks())
    with pytest.raises(ValueError):
        retriever.search("任意查询", top_k=0)


def test_search_on_empty_index_returns_empty():
    retriever = ListRetriever(FakeEmbedding(dim=64))
    assert retriever.search("任意查询") == []


def test_cosine_orthogonal_is_zero():
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_identical_is_one():
    assert _cosine([3.0, 4.0], [3.0, 4.0]) == pytest.approx(1.0)
