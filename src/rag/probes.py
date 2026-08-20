"""切分归因探测：用不同切分参数重建索引，辅助判断错误环节。

:class:`QdrantChunkingProbe` 供 :func:`rag.evaluate.diagnose_miss` 注入：
对每组 ``(chunk_size, overlap)`` 对照方案，在独立临时集合里重建索引并检索，
返回期望来源在各方案下的最高分；``close()`` 删除临时集合，不影响正式集合。

与正式建库共用同一 Embedder 与 Qdrant 客户端，保证向量空间与存储语义一致。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from .embeddings import Embedder
from .ingestion import build_chunks
from .qdrant_store import QdrantConfig, connect
from .retriever import QdrantRetriever

logger = logging.getLogger(__name__)

# 默认切分对照方案：(chunk_size, overlap)，均满足 overlap < chunk_size。
DEFAULT_VARIANTS = ((150, 30), (400, 80), (600, 120))


class QdrantChunkingProbe:
    """基于 Qdrant 临时集合的切分归因探测。

    Args:
        embedder: 与正式索引一致的嵌入器（保证向量空间可比）。
        base_config: 正式集合的 Qdrant 配置；临时集合只替换集合名。
        paths: 源文档路径列表（与建库阶段同一批原始资料）。
        variants: 对照切分方案，每项为 ``(chunk_size, overlap)``；
            缺省用 :data:`DEFAULT_VARIANTS`。

    Raises:
        ValueError: ``paths`` 为空，或 ``variants`` 含非法切分参数
            （``chunk_size <= 0``、``overlap < 0`` 或 ``overlap >= chunk_size``）。
    """

    def __init__(
        self,
        *,
        embedder: Embedder,
        base_config: QdrantConfig,
        paths: Iterable[str | Path],
        variants: Iterable[tuple[int, int]] | None = None,
    ) -> None:
        path_list = [Path(p) for p in paths]
        if not path_list:
            msg = "paths must not be empty"
            raise ValueError(msg)
        variant_list = list(variants) if variants is not None else list(DEFAULT_VARIANTS)
        for chunk_size, overlap in variant_list:
            if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
                msg = f"invalid chunk variant (chunk_size={chunk_size}, overlap={overlap})"
                raise ValueError(msg)
        self._embedder = embedder
        self._base_config = base_config
        self._paths = path_list
        self._variants = variant_list
        self._client = connect(base_config)
        self._retrievers: dict[tuple[int, int], QdrantRetriever] = {}

    @property
    def temp_collection_names(self) -> tuple[str, ...]:
        """本次探测已创建的临时集合名（供日志与测试观测）。"""
        return tuple(sorted({r.config.collection_name for r in self._retrievers.values()}))

    def best_scores(self, query: str, source: str, *, top_k: int = 5) -> dict[str, float]:
        """按各切分方案检索一次，返回 ``{方案描述: 该来源最高分}``。"""
        scores: dict[str, float] = {}
        for variant in self._variants:
            retriever = self._ensure_retriever(variant)
            hits = retriever.search(query, top_k=top_k, source_filter=source)
            scores[self._label(variant)] = max((r.score for r in hits), default=0.0)
        return scores

    def close(self) -> None:
        """删除全部临时集合并释放引用；可重复调用。"""
        for retriever in set(self._retrievers.values()):
            name = retriever.config.collection_name
            try:
                self._client.delete_collection(name)
            except Exception:
                logger.warning("chunking.probe.cleanup.failed collection=%s", name)
        self._retrievers.clear()
        logger.info("chunking.probe.closed")

    def __enter__(self) -> QdrantChunkingProbe:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _ensure_retriever(self, variant: tuple[int, int]) -> QdrantRetriever:
        retriever = self._retrievers.get(variant)
        if retriever is None:
            cfg = replace(self._base_config, collection_name=self._temp_name(variant))
            retriever = QdrantRetriever(self._embedder, cfg, client=self._client)
            chunks = build_chunks(self._paths, chunk_size=variant[0], overlap=variant[1])
            if chunks:
                retriever.index(chunks)
            self._retrievers[variant] = retriever
        return retriever

    def _temp_name(self, variant: tuple[int, int]) -> str:
        suffix = uuid.uuid4().hex[:8]
        return f"{self._base_config.collection_name}_probe_{variant[0]}_{variant[1]}_{suffix}"

    @staticmethod
    def _label(variant: tuple[int, int]) -> str:
        return f"{variant[0]}/{variant[1]}"
