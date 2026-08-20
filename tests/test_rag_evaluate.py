"""RAG 评测模块单元测试。

覆盖：
- load_dataset：正常加载 20 条检索集；异常（文件缺失 / 缺字段）。
- evaluate：用桩检索器验证 Recall@K 统计与未命中 / 误召回归类（确定性）。
- 端到端：FakeEmbedding + QdrantRetriever 小集跑 evaluate 不崩、指标在 0~1。
- 边界：空 query / 超长 query / OOV query 返回结构完整、不抛错。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rag.embeddings import FakeEmbedding
from rag.evaluate import EvalItem, diagnose_miss, evaluate, load_dataset
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


class _StubProbe:
    """返回预设分数的切分探测桩。"""

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores

    def best_scores(self, query: str, source: str, *, top_k: int = 5) -> dict[str, float]:
        return dict(self._scores)


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


class _FilterBugRetriever:
    """模拟过滤逻辑错误：``source_filter`` 被忽略（返回全部结果）。"""

    def search(
        self,
        query: str,
        top_k: int = 3,
        source_filter: str | None = None,
    ) -> list[RetrievalResult]:
        del query, source_filter
        return [_result("rag.md", 0.9), _result("qdrant.md", 0.5), _result("retriever.md", 0.3)][
            :top_k
        ]


def test_diagnose_miss_filter_bug() -> None:
    """过滤归因：source_filter 后仍返回其它来源 → 判定过滤逻辑错误。"""
    retriever = _FilterBugRetriever()
    results = retriever.search("q", top_k=3)
    diagnosis = diagnose_miss(retriever, "q", ("rag.md",), results)
    assert "过滤逻辑错误" in diagnosis
    assert "rag.md" in diagnosis


class _NoFilterRetriever:
    """模拟不支持来源过滤的检索器（ListRetriever 行为）。"""

    def search(
        self,
        query: str,
        top_k: int = 3,
        source_filter: str | None = None,
    ) -> list[RetrievalResult]:
        if source_filter is not None:
            raise TypeError("unsupported source_filter")
        return [_result("rag.md", 0.4)]


def test_diagnose_miss_unsupported_filter() -> None:
    """边界：检索器不支持 source_filter → 提示无法定位。"""
    retriever = _NoFilterRetriever()
    results = retriever.search("q", top_k=1)
    diagnosis = diagnose_miss(retriever, "q", ("rag.md",), results)
    assert "不支持来源过滤" in diagnosis


def _miss_inputs() -> tuple[_StubRetriever, list[RetrievalResult]]:
    stub = _StubRetriever({"q": [_result("rag.md", 0.40), _result("qdrant.md", 0.55)]})
    return stub, stub.search("q", top_k=2)


def test_diagnose_miss_chunking_probe_improves() -> None:
    """切分归因：换切分参数后最高分达到第 K 名 → 判定切分参数问题。"""
    stub, results = _miss_inputs()
    probe = _StubProbe({"150/30": 0.60, "400/80": 0.30})
    diagnosis = diagnose_miss(stub, "q", ("rag.md",), results, chunking_probe=probe)
    assert "切分参数问题" in diagnosis
    assert "150/30" in diagnosis


def test_diagnose_miss_chunking_probe_no_improve() -> None:
    """切分归因：换切分参数仍未达标 → 维持 Embedding 表达不足。"""
    stub, results = _miss_inputs()
    probe = _StubProbe({"150/30": 0.45, "400/80": 0.50})
    diagnosis = diagnose_miss(stub, "q", ("rag.md",), results, chunking_probe=probe)
    assert "Embedding 表达不足" in diagnosis
    assert "切分参数问题" not in diagnosis


class _BrokenProbe:
    """模拟探测执行异常（如 Embedding 服务不可用）。"""

    def best_scores(self, query: str, source: str, *, top_k: int = 5) -> dict[str, float]:
        raise ConnectionError("embedding service down")


def test_diagnose_miss_chunking_probe_raises() -> None:
    """异常：切分探测抛错 → 降级为 Embedding 归因并附注，不阻断。"""
    stub, results = _miss_inputs()
    diagnosis = diagnose_miss(stub, "q", ("rag.md",), results, chunking_probe=_BrokenProbe())
    assert "Embedding 表达不足" in diagnosis
    assert "切分探测执行异常" in diagnosis


def test_diagnose_miss_without_probe() -> None:
    """兼容：未注入探测 → 维持原 Embedding 归因。"""
    stub, results = _miss_inputs()
    diagnosis = diagnose_miss(stub, "q", ("rag.md",), results)
    assert "Embedding 表达不足" in diagnosis


class _UnfilteredEmptyRetriever:
    """模拟普通检索无结果但过滤检索可命中的异常检索器。"""

    def search(
        self,
        query: str,
        top_k: int = 3,
        source_filter: str | None = None,
    ) -> list[RetrievalResult]:
        del query, top_k
        if source_filter is not None:
            return [_result("rag.md", 0.9)]
        return []


def test_diagnose_miss_no_unfiltered_results() -> None:
    """边界：普通检索无任何结果但过滤可命中 → 提示检索链路异常。"""
    retriever = _UnfilteredEmptyRetriever()
    diagnosis = diagnose_miss(retriever, "q", ("rag.md",), [])
    assert "检索链路异常" in diagnosis


def test_evaluate_forwards_chunking_probe() -> None:
    """正常：evaluate 把 chunking_probe 透传给未命中归因。"""
    stub = _StubRetriever(
        {"q": [_result("a.md", 0.55), _result("b.md", 0.50), _result("rag.md", 0.40)]}
    )
    items = [EvalItem(query="q", expected_sources=("rag.md",))]
    probe = _StubProbe({"150/30": 0.60})
    summary, misses = evaluate(stub, items, top_ks=(2,), chunking_probe=probe)
    assert summary.answerable == 1
    assert misses
    assert "切分参数问题" in misses[0]["diagnosis"]


def test_evaluate_collect_details() -> None:
    """正常：detail_out 收集全部样本的检索明细与状态。"""
    stub = _StubRetriever(
        {
            "q-hit": [_result("rag.md", 0.9), _result("qdrant.md", 0.5)],
            "q-miss": [_result("rag.md", 0.4), _result("rag_concepts.md", 0.3)],
            "q-empty": [_result("rag.md", 0.2)],
        }
    )
    items = [
        EvalItem(query="q-hit", expected_sources=("rag.md",)),
        EvalItem(query="q-miss", expected_sources=("qdrant.md",)),
        EvalItem(query="q-empty", expected_sources=()),
    ]
    details: list[dict[str, Any]] = []
    evaluate(stub, items, top_ks=(1, 3), detail_out=details)
    assert len(details) == 3
    by_query = {d["query"]: d for d in details}
    assert by_query["q-hit"]["kind"] == "hit"
    assert by_query["q-hit"]["results"][0]["source"] == "rag.md"
    assert by_query["q-miss"]["kind"] == "miss"
    assert "diagnosis" in by_query["q-miss"]
    assert by_query["q-empty"]["kind"] == "false_recall"
