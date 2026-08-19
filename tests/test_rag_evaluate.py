"""RAG 评测模块单元测试。

覆盖：
- load_dataset：正常加载 20 条检索集；异常（文件缺失 / 缺字段）。
- evaluate：用桩检索器验证 Recall@K 统计与未命中 / 误召回归类（确定性）。
- 端到端：FakeEmbedding + QdrantRetriever 小集跑 evaluate 不崩、指标在 0~1。
- 边界：空 query / 超长 query / OOV query 返回结构完整、不抛错。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.embeddings import FakeEmbedding
from rag.evaluate import EvalItem, evaluate, load_dataset
from rag.ingestion import Chunk
from rag.qdrant_store import QdrantConfig
from rag.retriever import QdrantRetriever, RetrievalResult

_REPO = Path(__file__).resolve().parent.parent
_DATASET = _REPO / "examples" / "retrieval_set.json"


def _result(source: str, score: float) -> RetrievalResult:
    return RetrievalResult(chunk_id=f"{source}#0", source=source, score=score, text="t")


class _StubRetriever:
    """返回预设结果的检索器桩，用于确定性验证统计逻辑。"""

    def __init__(self, mapping: dict[str, list[RetrievalResult]]) -> None:
        self._mapping = mapping

    def search(
        self,
        query: str,
        top_k: int = 3,
        source_filter: str | None = None,
    ) -> list[RetrievalResult]:
        results = list(self._mapping[query])[:top_k]
        if source_filter is not None:
            results = [r for r in results if r.source == source_filter]
        return results


def test_load_dataset_real_file() -> None:
    """正常：加载真实检索集，20 条且字段合法。"""
    items = load_dataset(_DATASET)
    assert len(items) == 20
    for item in items:
        assert item.query.strip()
        assert isinstance(item.expected_sources, tuple)


def test_load_dataset_missing_file() -> None:
    """异常：文件不存在 → FileNotFoundError。"""
    with pytest.raises(FileNotFoundError, match="retrieval set not found"):
        load_dataset(_REPO / "datasets" / "nope.json")


def test_load_dataset_bad_item(tmp_path: Path) -> None:
    """异常：条目缺 query → ValueError。"""
    p = tmp_path / "bad.json"
    p.write_text('[{"expected_sources": []}]', encoding="utf-8")
    with pytest.raises(ValueError, match="'query' must be a non-empty string"):
        load_dataset(p)


def test_evaluate_stats_with_stub() -> None:
    """正常：桩检索器下 Recall@1/3/5 统计与 miss/false_recall 归类正确。"""
    stub = _StubRetriever(
        {
            "q-hit": [_result("rag.md", 0.9), _result("qdrant.md", 0.5)],
            "q-miss": [_result("rag.md", 0.4), _result("rag_concepts.md", 0.3)],
            "q-empty": [_result("rag.md", 0.2)],  # 无答案样本但被误召回
        }
    )
    items = [
        EvalItem(query="q-hit", expected_sources=("rag.md",), note="命中"),
        EvalItem(query="q-miss", expected_sources=("qdrant.md",), note="未命中"),
        EvalItem(query="q-empty", expected_sources=(), note="无答案"),
    ]
    summary, misses = evaluate(stub, items, top_ks=(1, 3, 5))
    assert summary.total == 3
    assert summary.answerable == 2
    assert summary.unanswerable == 1
    assert summary.hits[1] == 1  # 仅 q-hit 第 1 名命中
    assert summary.hits[3] == 1
    assert summary.hits[5] == 1
    assert summary.recall[1] == pytest.approx(0.5)
    assert summary.recall[3] == pytest.approx(0.5)
    assert summary.false_recalled == 1

    kinds = {m["kind"] for m in misses}
    assert kinds == {"miss", "false_recall"}
    miss = next(m for m in misses if m["kind"] == "miss")
    assert miss["query"] == "q-miss"
    assert "qdrant.md" in miss["diagnosis"]


def test_evaluate_empty_items() -> None:
    """边界：空评测集 → 统计为零，不抛错。"""
    stub = _StubRetriever({})
    summary, misses = evaluate(stub, [], top_ks=(1, 3))
    assert summary.total == 0
    assert summary.answerable == 0
    assert summary.recall == {1: 0.0, 3: 0.0}
    assert misses == []


def _qdrant_retriever() -> QdrantRetriever:
    chunks = [
        Chunk(
            chunk_id="rag.md#0",
            source="rag.md",
            text="RAG 是检索增强生成，检索阶段召回相关片段。",
            metadata={"chunk_index": 0, "char_start": 0},
        ),
        Chunk(
            chunk_id="qdrant.md#0",
            source="qdrant.md",
            text="Qdrant 是开源向量数据库，保存向量与元数据。",
            metadata={"chunk_index": 0, "char_start": 0},
        ),
    ]
    emb = FakeEmbedding(dim=16)
    cfg = QdrantConfig(mode="local", path=":memory:", collection_name="eval_rag", vector_size=16)
    retriever = QdrantRetriever(emb, cfg)
    retriever.index(chunks)
    return retriever


def test_evaluate_end_to_end_qdrant() -> None:
    """正常：FakeEmbedding + Qdrant 小集端到端评测，指标在 0~1。"""
    retriever = _qdrant_retriever()
    items = [
        EvalItem(query="向量数据库", expected_sources=("qdrant.md",)),
        EvalItem(query="检索增强生成", expected_sources=("rag.md",)),
    ]
    summary, misses = evaluate(retriever, items, top_ks=(1, 3))
    assert summary.answerable == 2
    for k in (1, 3):
        assert 0.0 <= summary.recall[k] <= 1.0
    assert isinstance(misses, list)


def test_search_empty_query_returns_structure() -> None:
    """边界：空 query 不抛错，返回列表（可能为空）。"""
    retriever = _qdrant_retriever()
    results = retriever.search("", top_k=3)
    assert isinstance(results, list)


def test_search_very_long_query_returns_structure() -> None:
    """边界：超长 query 不抛错，返回结构完整。"""
    retriever = _qdrant_retriever()
    long_query = "检索 " * 5000
    results = retriever.search(long_query, top_k=3)
    assert isinstance(results, list)
    assert all(isinstance(r.score, float) for r in results)


def test_search_oov_query_returns_structure() -> None:
    """边界：完全无语义相关（乱码）query 不抛错，返回列表。"""
    retriever = _qdrant_retriever()
    results = retriever.search("zxqjklmno 9x7y6z5w", top_k=3)
    assert isinstance(results, list)
