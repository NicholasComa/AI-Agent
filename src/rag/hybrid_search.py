"""混合检索：BM25 稀疏路 + 向量稠密路 RRF 融合；含精排适配器。

- :func:`tokenize` / :class:`BigramBM25`：不依赖分词器的字符 Bigram BM25
  内存索引（中文按相邻汉字二元组切分，ASCII 按整词切分），与向量检索
  形成互补信号，补足专有名词与精确词匹配；索引按 ``chunk_id`` 去重，
  重复 ``index(chunks)`` 不会累积条目；
- :class:`HybridRetriever`：向量 + BM25 双路召回，RRF（Reciprocal Rank
  Fusion）融合排序；空 BM25 索引时自动从底层向量库 scroll 重建，无需
  重跑导入；返回结果 ``score`` 字段为「向量余弦 / BM25 归一化分」的
  较大值，与 ``--min-score`` 阈值语义一致；
- :class:`RerankRetriever`：把「粗召回 + 精排」包装成可评估的检索器，
  使 ``rag.evaluate`` 等只认识 ``search(query, top_k)`` 的调用方可直接使用。
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

from rank_bm25 import BM25Okapi

from rag.ingestion import Chunk
from rag.metadata_filter import MetadataConditions
from rag.rerank import Reranker
from rag.retriever import QdrantRetriever, RetrievalResult

logger = logging.getLogger(__name__)

_ASCII_WORD = re.compile(r"[a-zA-Z0-9_]+")


def tokenize(text: str) -> list[str]:
    """把文本切为 token 列表：ASCII 整词 + 中文汉字 bigram。"""
    tokens = [word.lower() for word in _ASCII_WORD.findall(text)]
    cjk = "".join(c for c in text if "\u4e00" <= c <= "\u9fff")
    tokens.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))
    return tokens


class BigramBM25:
    """基于字符 Bigram 的 BM25 检索器（内存索引）。

    内部用 ``dict[str, Chunk]`` 存储语料，``index(chunks)`` 按
    ``chunk_id`` 覆盖合并（重复调用不会让文档数翻倍）；``BM25Okapi``
    实例在 ``search`` 时按需懒构建。
    """

    def __init__(self) -> None:
        self._docs: dict[str, Chunk] = {}
        self._index: BM25Okapi | None = None
        self._dirty = True

    def __len__(self) -> int:
        return len(self._docs)

    def index(self, chunks: list[Chunk]) -> int:
        """按 ``chunk_id`` 合并一批 ``Chunk`` 到索引，并标记脏位。"""
        for chunk in chunks:
            self._docs[chunk.chunk_id] = chunk
        self._dirty = True
        return len(self._docs)

    def clear(self) -> None:
        """清空索引（用于 ``rebuild`` 场景）。"""
        self._docs.clear()
        self._index = None
        self._dirty = True

    def _ensure_built(self) -> None:
        """按需重建 ``BM25Okapi``（仅当 ``_dirty`` 为真）。"""
        if not self._dirty:
            return
        if not self._docs:
            self._index = None
        else:
            corpus = [tokenize(chunk.text) for chunk in self._docs.values()]
            self._index = BM25Okapi(corpus)
        self._dirty = False

    def search(self, query: str, top_k: int = 3) -> list[RetrievalResult]:
        """按 BM25 分数返回 Top-K 结果。

        未建索引调用时抛 :class:`RuntimeError`（保持历史行为，便于诊断）。
        """
        if top_k <= 0:
            msg = f"top_k must be > 0, got {top_k}"
            raise ValueError(msg)
        self._ensure_built()
        if self._index is None:
            msg = "BigramBM25 has no index; call index(chunks) first"
            raise RuntimeError(msg)
        scores = self._index.get_scores(tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        results: list[RetrievalResult] = []
        # ``_docs`` 保留插入顺序，索引按此顺序构造。
        chunks_ordered = list(self._docs.values())
        for i in order:
            if len(results) >= top_k:
                break
            chunk = chunks_ordered[i]
            results.append(
                RetrievalResult(
                    chunk_id=chunk.chunk_id,
                    source=chunk.source,
                    score=float(scores[i]),
                    text=chunk.text,
                )
            )
        return results


class HybridRetriever:
    """混合检索：向量 + BM25 双路召回，RRF 融合排序。

    两路各取 ``top_candidates`` 条，按 Reciprocal Rank Fusion
    （``score = Σ 1 / (k + rank)``）合并后取 Top-K；两路索引共用
    同一批 ``Chunk``，chunk_id 对齐保证可融合。

    当 BM25 索引为空时，首次 ``search`` 会尝试从底层 ``QdrantRetriever``
    scroll 出全部点重建索引（无需重跑导入）。结果 ``score`` 字段统一为
    ``max(向量余弦, BM25归一化)``，使 ``--min-score`` 阈值在混合模式下
    仍按 0-1 余弦口径生效。
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
        self._bm25_loaded = False

    def index(self, chunks: list[Chunk]) -> int:
        """同时喂给向量路与 BM25 路；BM25 按 ``chunk_id`` 去重。"""
        vector_count = self._vector.index(chunks)
        self._bm25.index(chunks)
        self._bm25_loaded = True
        return vector_count

    def clear_index(self) -> None:
        """清空 BM25 索引（向量库不动），配合 ``rag.rebuild()`` 使用。"""
        self._bm25.clear()
        self._bm25_loaded = False

    def __len__(self) -> int:
        """返回底层向量集合中的点数量。"""
        return len(self._vector)

    def _maybe_rebuild_bm25(self) -> None:
        """首次 search 时若 BM25 为空，从向量库反推语料重建。"""
        if self._bm25_loaded:
            return
        if len(self._bm25) > 0:
            self._bm25_loaded = True
            return
        if len(self._vector) == 0:
            return  # 空集合，无需重试
        try:
            chunks = self._vector.load_chunks()
        except Exception as exc:  # noqa: BLE001 - 任意失败都回退纯向量
            logger.warning("hybrid.bm25.auto_load_failed err=%s", exc)
            self._bm25_loaded = True  # 不再重试
            return
        self._bm25.index(chunks)
        self._bm25_loaded = True
        logger.info("hybrid.bm25.auto_loaded chunks=%d", len(chunks))

    def search(
        self,
        query: str,
        top_k: int = 3,
        *,
        source_filter: str | None = None,
        metadata: MetadataConditions | None = None,
    ) -> list[RetrievalResult]:
        """双路召回 + RRF 融合，返回 Top-K。

        返回结果 ``score`` 字段为 ``max(向量余弦, BM25归一化)``，融合后的
        排序仍由 RRF 决定，与分数无关。
        """
        if top_k <= 0:
            msg = f"top_k must be > 0, got {top_k}"
            raise ValueError(msg)
        self._maybe_rebuild_bm25()
        vec_hits = self._vector.search(
            query,
            top_k=self._top_candidates,
            source_filter=source_filter,
            metadata=metadata,
        )
        try:
            bm25_hits = self._bm25.search(query, top_k=self._top_candidates)
        except RuntimeError:
            bm25_hits = []

        vec_score: dict[str, float] = {r.chunk_id: r.score for r in vec_hits}
        bm_score: dict[str, float] = {r.chunk_id: r.score for r in bm25_hits}
        max_bm = max(bm_score.values(), default=0.0)
        if max_bm <= 0.0:
            max_bm = 1.0  # 仅出现 BM25 全 0 时避免除零，归一化结果即为 0

        fused: dict[str, float] = {}
        chosen: dict[str, RetrievalResult] = {}
        for hits in (vec_hits, bm25_hits):
            for rank, result in enumerate(hits):
                if result.chunk_id not in chosen:
                    chosen[result.chunk_id] = result
                fused[result.chunk_id] = fused.get(result.chunk_id, 0.0) + 1.0 / (
                    self.RRF_K + rank + 1
                )
        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)

        results: list[RetrievalResult] = []
        for chunk_id, _ in ranked[:top_k]:
            base = chosen[chunk_id]
            vs = vec_score.get(chunk_id)
            bs = bm_score.get(chunk_id)
            score = 0.0
            if vs is not None:
                score = vs
            if bs is not None:
                score = max(score, bs / max_bm)
            results.append(replace(base, score=score))
        return results


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
