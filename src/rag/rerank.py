"""精排层：对粗召回候选重新打分取 Top-K。

- :class:`EmbeddingReranker`：轻量实现，用 Embedding 模型对 query 与
  每个候选重新编码并计算余弦相似度，重排后取 Top-K（分数同步更新）。
  候选表示可通过 ``repr_fn`` 定制（首句 / 摘要等）。
- :class:`CrossEncoderReranker`：占位，说明真实 Cross-Encoder 的接入
  方式，调用 ``rerank`` 时抛出 :class:`NotImplementedError`。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from rag.embeddings import Embedder
from rag.retriever import RetrievalResult

ReprFn = Callable[[str], str]


class Reranker(Protocol):
    """精排器接口：把候选列表按与 query 的相关度重排。"""

    def rerank(
        self,
        query: str,
        candidates: list[RetrievalResult],
        top_k: int,
    ) -> list[RetrievalResult]: ...


def _cosine(a: list[float], b: list[float]) -> float:
    """两个向量之间的余弦相似度（模长为零时返回 0）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbeddingReranker:
    """用 Embedding 模型对候选重新打分（轻量复排，零额外依赖）。"""

    def __init__(self, embedder: Embedder, repr_fn: ReprFn | None = None) -> None:
        self._embedder = embedder
        self._repr_fn = repr_fn or (lambda text: text)

    def rerank(
        self,
        query: str,
        candidates: list[RetrievalResult],
        top_k: int = 3,
    ) -> list[RetrievalResult]:
        """对候选逐条重新打分并取 Top-K；返回结果按新分数降序、分数已更新。"""
        if top_k <= 0:
            msg = f"top_k must be > 0, got {top_k}"
            raise ValueError(msg)
        if not candidates:
            return []
        query_vec = self._embedder.embed([self._repr_fn(query)])[0]
        vectors = self._embedder.embed([self._repr_fn(c.text) for c in candidates])
        scored: list[tuple[float, RetrievalResult]] = []
        for result, vec in zip(candidates, vectors, strict=False):
            score = _cosine(query_vec, vec)
            scored.append((score, replace(result, score=score)))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [result for _, result in scored[:top_k]]


class CrossEncoderReranker:
    """Cross-Encoder 精排占位：真实接入需 sentence-transformers 与 HF 模型。

    接入方式（供后续实现参考）：加载 cross-encoder 模型后对
    ``(query, candidate.text)`` 逐对打分，按分数重排；本类暂未接入，
    调用 :meth:`rerank` 抛出 :class:`NotImplementedError`。
    """

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2") -> None:
        self._model_name = model_name

    def rerank(
        self,
        query: str,
        candidates: list[RetrievalResult],
        top_k: int = 3,
    ) -> list[RetrievalResult]:
        msg = "CrossEncoderReranker is not implemented; use EmbeddingReranker"
        raise NotImplementedError(msg)
