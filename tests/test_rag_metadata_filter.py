"""rag.metadata_filter 单元测试：Metadata 过滤条件编译为 Qdrant Filter。"""

from __future__ import annotations

from rag.embeddings import FakeEmbedding
from rag.ingestion import Chunk
from rag.metadata_filter import build_filter, file_type_is, source_is
from rag.qdrant_store import QdrantConfig
from rag.retriever import QdrantRetriever


def _chunks() -> list[Chunk]:
    return [
        Chunk(
            chunk_id="a.md#0",
            source="a.md",
            text="RAG 是检索增强生成。",
            metadata={"chunk_index": 0, "char_start": 0},
        ),
        Chunk(
            chunk_id="b.pdf#0",
            source="b.pdf",
            text="Qdrant 是一个开源向量数据库。",
            metadata={"chunk_index": 0, "char_start": 0, "file_type": "pdf", "page": 2},
        ),
    ]


def test_build_filter_str_value_uses_match_value() -> None:
    """正常：str 值编译为 MatchValue 精确匹配。"""
    qf = build_filter({"source": "a.md"})
    assert qf is not None
    assert len(qf.must) == 1
    field = qf.must[0]
    assert field.key == "source"
    assert field.match.value == "a.md"


def test_build_filter_list_value_uses_match_any_and_merges_source() -> None:
    """正常：list 值编译为 MatchAny；metadata 与 source_filter 按 AND 合并。"""
    qf = build_filter({"file_type": ["pdf", "text"]}, source_filter="b.md")
    assert qf is not None
    keys = {field.key for field in qf.must}
    assert keys == {"file_type", "source"}
    file_field = next(f for f in qf.must if f.key == "file_type")
    assert sorted(file_field.match.any) == ["pdf", "text"]


def test_build_filter_none_returns_none_and_helpers() -> None:
    """边界：无任何条件返回 None（全量检索）；helper 构造正确条件。"""
    assert build_filter(None) is None
    assert build_filter({}) is None
    assert source_is("a.md", "b.md") == {"source": ["a.md", "b.md"]}
    assert file_type_is("pdf") == {"file_type": ["pdf"]}


def test_retriever_search_with_metadata_filters_pdf() -> None:
    """集成：QdrantRetriever.search 用 metadata 过滤只返回 pdf 片段。"""
    cfg = QdrantConfig(mode="local", path=":memory:", collection_name="filters_api", vector_size=8)
    retriever = QdrantRetriever(FakeEmbedding(dim=8), cfg)
    retriever.index(_chunks())
    results = retriever.search("数据库", top_k=10, metadata={"file_type": ["pdf"]})
    assert results
    assert all(r.source.endswith(".pdf") for r in results)
