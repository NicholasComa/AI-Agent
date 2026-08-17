"""最小相似度检索。

先用 **Python 列表** 实现最小相似度检索（:class:`ListRetriever`），再在后续迁移到
Qdrant。检索接口返回 ``chunk_id`` / ``source`` / ``score`` / ``text``，
**不调用任何 LLM** —— 本模块只负责召回，生成（回答）不在本模块职责内。

相似度用余弦相似度：两个向量点积除以各自模长的乘积；模长为零时记为 0 分。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .embeddings import Embedder
from .ingestion import Chunk


@dataclass(frozen=True)
class RetrievalResult:
    """一条检索结果。"""

    chunk_id: str
    source: str
    score: float
    text: str


def _cosine(a: list[float], b: list[float]) -> float:
    """两个向量之间的余弦相似度（模长为零时返回 0）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class ListRetriever:
    """基于内存列表 + 余弦相似度的最小检索器。

    把全部片段的向量存在内存里，检索时对 query 向量与每个片段向量算余弦相似度，
    按分数降序取 Top-K。
    """

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self._chunks: list[Chunk] = []
        self._vectors: list[list[float]] = []

    def __len__(self) -> int:
        return len(self._chunks)

    def index(self, chunks: list[Chunk]) -> None:
        """为一批片段建立索引（向量化并存入内存）。"""
        self._chunks = list(chunks)
        self._vectors = self._embedder.embed([chunk.text for chunk in self._chunks])

    def search(self, query: str, top_k: int = 3) -> list[RetrievalResult]:
        """检索与 ``query`` 最相似的 Top-K 个片段。

        Args:
            query: 查询文本。
            top_k: 返回的片段数量（``<= 0`` 抛 :class:`ValueError`）。

        Returns:
            按 ``score`` 降序的 :class:`RetrievalResult` 列表。
        """
        if top_k <= 0:
            msg = f"top_k must be > 0, got {top_k}"
            raise ValueError(msg)

        q = self._embedder.embed([query])[0]
        scored = [
            RetrievalResult(
                chunk_id=chunk.chunk_id,
                source=chunk.source,
                score=_cosine(q, vec),
                text=chunk.text,
            )
            for chunk, vec in zip(self._chunks, self._vectors, strict=False)
        ]
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]
