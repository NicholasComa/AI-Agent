"""QdrantChunkingProbe 单元测试。

覆盖：
- best_scores：返回全部方案、值为 [0,1]；语义匹配的来源分数明显高；
  索引中不存在的来源记 0。
- 生命周期：close 清空临时集合记录且可重复调用。
- 边界：空 paths / 非法切分参数 → ValueError；维度不一致 → ValueError。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.embeddings import FakeEmbedding
from rag.probes import DEFAULT_VARIANTS, QdrantChunkingProbe
from rag.qdrant_store import QdrantConfig

_DOC_TEXT = "RAG 是检索增强生成。检索阶段负责召回相关片段。" * 20


def _make_probe(
    tmp_path: Path,
    *,
    variants: list[tuple[int, int]] | None = None,
    embedder_dim: int = 16,
    vector_size: int = 16,
) -> QdrantChunkingProbe:
    doc = tmp_path / "rag.md"
    doc.write_text(_DOC_TEXT, encoding="utf-8")
    emb = FakeEmbedding(dim=embedder_dim)
    cfg = QdrantConfig(
        mode="local", path=":memory:", collection_name="probe_main", vector_size=vector_size
    )
    return QdrantChunkingProbe(embedder=emb, base_config=cfg, paths=[doc], variants=variants)


def test_best_scores_returns_all_variants(tmp_path: Path) -> None:
    """正常：返回全部方案的分数，且取值在 [0, 1]。"""
    probe = _make_probe(tmp_path)
    scores = probe.best_scores("检索阶段负责召回相关片段", "rag.md")
    assert set(scores) == {f"{cs}/{ov}" for cs, ov in DEFAULT_VARIANTS}
    for value in scores.values():
        assert 0.0 <= value <= 1.0
    probe.close()


def test_best_scores_matching_source_high(tmp_path: Path) -> None:
    """正常：词面高度重合的查询，该来源最高分应明显高于 0。"""
    probe = _make_probe(tmp_path, variants=[(150, 30)])
    scores = probe.best_scores("检索阶段负责召回相关片段", "rag.md")
    assert max(scores.values()) > 0.4
    probe.close()


def test_best_scores_missing_source_zero(tmp_path: Path) -> None:
    """边界：索引中不存在该来源 → 全部方案记 0。"""
    probe = _make_probe(tmp_path)
    scores = probe.best_scores("检索", "other.md")
    assert all(value == 0.0 for value in scores.values())
    probe.close()


def test_close_clears_temp_collections(tmp_path: Path) -> None:
    """正常：close 删除临时集合记录，且可重复调用不抛错。"""
    probe = _make_probe(tmp_path, variants=[(150, 30)])
    probe.best_scores("检索", "rag.md")
    assert probe.temp_collection_names
    probe.close()
    assert probe.temp_collection_names == ()
    probe.close()  # 幂等


def test_empty_paths_raises() -> None:
    """边界：paths 为空 → ValueError。"""
    cfg = QdrantConfig(mode="local", path=":memory:", collection_name="main", vector_size=16)
    with pytest.raises(ValueError, match="paths must not be empty"):
        QdrantChunkingProbe(embedder=FakeEmbedding(dim=16), base_config=cfg, paths=[])


def test_invalid_variant_raises(tmp_path: Path) -> None:
    """边界：overlap >= chunk_size → ValueError。"""
    with pytest.raises(ValueError, match="invalid chunk variant"):
        _make_probe(tmp_path, variants=[(100, 100)])


def test_best_scores_dim_mismatch_raises(tmp_path: Path) -> None:
    """异常：Embedding 维度与集合 vector_size 不一致 → ValueError。"""
    probe = _make_probe(tmp_path, embedder_dim=8, vector_size=16)
    with pytest.raises(ValueError, match="dim"):
        probe.best_scores("检索", "rag.md")
    probe.close()
