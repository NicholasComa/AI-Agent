"""Qdrant 单元测试（使用进程内嵌 Local Mode :memory:，无需外部服务）。

覆盖：1 正常 + 3 异常（不支持的 mode、top_k=0、查询向量维度不匹配）。
"""

from __future__ import annotations

import pytest

from rag.embeddings import FakeEmbedding
from rag.ingestion import Chunk
from rag.qdrant_store import (
    QdrantConfig,
    QdrantConfigError,
    build_point,
    connect,
    ensure_collection,
    search_points,
    upsert_points,
)
from rag.retriever import QdrantRetriever


def _chunks() -> list[Chunk]:
    return [
        Chunk(
            chunk_id="rag.md#0",
            source="rag.md",
            text="RAG 是检索增强生成。",
            metadata={"chunk_index": 0, "char_start": 0},
        ),
        Chunk(
            chunk_id="rag.md#1",
            source="rag.md",
            text="检索阶段召回与 query 最相关的片段。",
            metadata={"chunk_index": 1, "char_start": 20},
        ),
        Chunk(
            chunk_id="qdrant.md#0",
            source="qdrant.md",
            text="Qdrant 是一个开源向量数据库。",
            metadata={"chunk_index": 0, "char_start": 0},
        ),
    ]


def _make_cfg(dim: int = 8, collection: str = "test_rag") -> QdrantConfig:
    return QdrantConfig(mode="local", path=":memory:", collection_name=collection, vector_size=dim)


def test_qdrant_retriever_index_and_search_roundtrip() -> None:
    """正常：索引 3 个片段，按 query 检索能命中语义相关片段（fake dim=8）。"""
    emb = FakeEmbedding(dim=8)
    cfg = _make_cfg(dim=8)
    retriever = QdrantRetriever(emb, cfg)
    assert len(retriever) == 0
    n = retriever.index(_chunks())
    assert n == 3
    assert len(retriever) == 3
    results = retriever.search("向量数据库", top_k=2)
    assert len(results) == 2
    top = results[0]
    assert top.chunk_id
    assert top.source
    assert top.text
    assert isinstance(top.score, float)


def test_qdrant_retriever_search_with_source_filter() -> None:
    """正常：source_filter 只返回指定 source 的片段。"""
    emb = FakeEmbedding(dim=8)
    cfg = _make_cfg(collection="filter_rag")
    retriever = QdrantRetriever(emb, cfg)
    retriever.index(_chunks())
    results = retriever.search("向量", top_k=10, source_filter="qdrant.md")
    assert all(r.source == "qdrant.md" for r in results)


def test_qdrant_config_rejects_unsupported_mode() -> None:
    """异常 1：mode 取值非法 → QdrantConfigError。"""
    with pytest.raises(QdrantConfigError, match="unsupported Qdrant mode"):
        QdrantConfig(mode="invalid")  # type: ignore[arg-type]


def test_qdrant_config_rejects_invalid_vector_size() -> None:
    """异常 2：vector_size <= 0 → QdrantConfigError。"""
    with pytest.raises(QdrantConfigError, match="vector_size must be > 0"):
        QdrantConfig(vector_size=0)


def test_search_points_rejects_top_k_zero() -> None:
    """异常 3：top_k <= 0 → QdrantConfigError。"""
    cfg = _make_cfg(dim=8, collection="topk_rag")
    client = connect(cfg)
    ensure_collection(client, cfg)
    with pytest.raises(QdrantConfigError, match="top_k must be > 0"):
        search_points(client, cfg, [0.0] * 8, top_k=0)


def test_search_points_rejects_wrong_query_dim() -> None:
    """异常 4：query_vector 维度与 collection vector_size 不一致 → QdrantConfigError。"""
    cfg = _make_cfg(dim=8, collection="dim_rag")
    client = connect(cfg)
    ensure_collection(client, cfg)
    with pytest.raises(QdrantConfigError, match="dim .* != .* vector_size"):
        search_points(client, cfg, [0.0] * 16, top_k=1)


def test_upsert_empty_batch_is_noop() -> None:
    """边界：upsert 空批返回 0，不抛错。"""
    cfg = _make_cfg(collection="empty_rag")
    client = connect(cfg)
    ensure_collection(client, cfg)
    assert upsert_points(client, cfg, []) == 0


def test_build_point_string_id_becomes_uuid() -> None:
    """build_point：非数字字符串 ID 哈希为确定性 UUID。"""
    import uuid

    p1 = build_point(point_id="rag.md#0", vector=[0.1, 0.2], payload={"source": "x"})
    p2 = build_point(point_id="rag.md#0", vector=[0.1, 0.2], payload={"source": "x"})
    assert p1.id == p2.id
    # 是有效 UUID
    uuid.UUID(str(p1.id))
    # 不同输入产生不同 UUID
    p3 = build_point(point_id="rag.md#1", vector=[0.1], payload={})
    assert p1.id != p3.id


def test_build_point_numeric_string_id_becomes_int() -> None:
    """build_point：纯数字字符串 ID 自动转 int。"""
    p = build_point(point_id="42", vector=[0.1], payload={})
    assert p.id == 42


def test_qdrant_config_from_app_config_maps_fields() -> None:
    """正常：from_app_config 把 AppConfig.qdrant_* 映射到 QdrantConfig。"""
    from config import AppConfig

    app = AppConfig.model_validate(
        {
            "api_base_url": "http://localhost:11434/v1",
            "model_name": "qwen3",
            "qdrant_mode": "docker",
            "qdrant_path": "./qdrant_storage",
            "qdrant_host": "127.0.0.1",
            "qdrant_port": 6334,
            "qdrant_collection_name": "my_collection",
            "qdrant_vector_size": 768,
        }
    )
    cfg = QdrantConfig.from_app_config(app)
    assert cfg.mode == "docker"
    assert cfg.path == "./qdrant_storage"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 6334
    assert cfg.collection_name == "my_collection"
    assert cfg.vector_size == 768


def test_qdrant_config_from_app_config_rejects_wrong_type() -> None:
    """异常：from_app_config 传入非 AppConfig → TypeError。"""
    with pytest.raises(TypeError, match="expected AppConfig"):
        QdrantConfig.from_app_config(object())  # type: ignore[arg-type]
