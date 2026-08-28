"""检索增强组合：混合检索与轻量精排适配。

- :class:`HybridRetriever`：向量 + BM25 双路召回，RRF 融合排序；
- :class:`RerankRetriever`：把「粗召回 + 精排」包装成可评估的检索器，
  使 ``rag.evaluate`` 等只认识 ``search(query, top_k)`` 的调用方可直接使用。
"""

from __future__ import annotations

from rag.bm25 import BigramBM25
from rag.filters import MetadataConditions
from rag.ingestion import Chunk
from rag.rerank import Reranker
from rag.retriever import QdrantRetriever, RetrievalResult


class HybridRetriever:
    """混合检索：向量 + BM25 双路召回，RRF 融合排序。

    两路各取 ``top_candidates`` 条，按 Reciprocal Rank Fusion
    （``score = Σ 1 / (k + rank)``）合并后取 Top-K；两路索引共用
    同一批 ``Chunk``，chunk_id 对齐保证可融合。
    """

    RRF_K = 60

    def __init__(
        self,
        vector: QdrantRetriever,
        bm25: BigramBM25,
        *,
        top_candidates: int = 30,
    ) -> None:
        self._vector = vector
        self._bm25 = bm25
        self._top_candidates = top_candidates

    def index(self, chunks: list[Chunk]) -> int:
        """同时喂给向量路与 BM25 路。"""
        vector_count = self._vector.index(chunks)
        self._bm25.index(chunks)
        return vector_count

    def __len__(self) -> int:
        """返回底层向量集合中的点数量。"""
        return len(self._vector)

    def search(
        self,
        query: str,
        top_k: int = 3,
        *,
        source_filter: str | None = None,
        metadata: MetadataConditions | None = None,
    ) -> list[RetrievalResult]:
        """双路召回 + RRF 融合，返回 Top-K。"""
        if top_k <= 0:
            msg = f"top_k must be > 0, got {top_k}"
            raise ValueError(msg)
        vec_hits = self._vector.search(
            query,
            top_k=self._top_candidates,
            source_filter=source_filter,
            metadata=metadata,
        )
        bm25_hits = self._bm25.search(query, top_k=self._top_candidates)
        fused: dict[str, tuple[float, RetrievalResult]] = {}
        for hits in (vec_hits, bm25_hits):
            for rank, result in enumerate(hits):
                score = 1.0 / (self.RRF_K + rank + 1)
                prev = fused.get(result.chunk_id)
                if prev is None:
                    fused[result.chunk_id] = (score, result)
                else:
                    fused[result.chunk_id] = (prev[0] + score, result)
        ranked = sorted(fused.values(), key=lambda item: item[0], reverse=True)
        return [result for _, result in ranked[:top_k]]


class RerankRetriever:
    """包装「粗召回 + 精排」：先取 Top-N 候选，再由精排器重新打分取 Top-K。"""

    def __init__(
        self,
        base: QdrantRetriever,
        reranker: Reranker,
        *,
        coarse_top_k: int = 20,
    ) -> None:
        self._base = base
        self._reranker = reranker
        self._coarse_top_k = coarse_top_k

    def index(self, chunks: list[Chunk]) -> int:
        return self._base.index(chunks)

    def __len__(self) -> int:
        """返回底层向量集合中的点数量。"""
        return len(self._base)

    def search(
        self,
        query: str,
        top_k: int = 3,
        *,
        source_filter: str | None = None,
        metadata: MetadataConditions | None = None,
    ) -> list[RetrievalResult]:
        """粗召回（含过滤条件）→ 精排 → Top-K。"""
        coarse = self._base.search(
            query,
            top_k=self._coarse_top_k,
            source_filter=source_filter,
            metadata=metadata,
        )
        return self._reranker.rerank(query, coarse, top_k)
