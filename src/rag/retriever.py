"""最小相似度检索。

提供两种实现：

- :class:`ListRetriever` — 内存列表 + 余弦相似度，用于本地原型与单元测试。
- :class:`QdrantRetriever` — 进程内嵌或远程 Qdrant，用于生产部署。

检索接口统一返回 ``chunk_id`` / ``source`` / ``score`` / ``text``，**不调用任何
LLM** —— 本模块只负责召回，生成（回答）不在本模块职责内。

相似度用余弦相似度：两个向量点积除以各自模长的乘积；模长为零时记为 0 分。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .embeddings import Embedder
from .ingestion import Chunk

if TYPE_CHECKING:
    from .qdrant_store import QdrantClient, QdrantConfig

logger = logging.getLogger(__name__)


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
        logger.info(
            "retriever.list.indexed count=%d dim=%d",
            len(self._chunks),
            len(self._vectors[0]) if self._vectors else 0,
        )

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


class QdrantRetriever:
    """基于 Qdrant 的检索器（嵌入式 Local Mode 或远程 Docker）。

    构造时即连接并确保集合存在；``index`` 把一批片段向量化后写入 Qdrant，
    ``search`` 用同一 Embedder 把 query 向量化后做 Top-K 检索。
    """

    def __init__(
        self,
        embedder: Embedder,
        config: QdrantConfig,
        client: QdrantClient | None = None,
    ) -> None:
        """构造检索器。

        Args:
            embedder: 与集合维度一致的嵌入器。
            config: Qdrant 连接与集合配置。
            client: 可选，复用外部 Qdrant 客户端（例如在同一 ``:memory:``
                实例上建立多个临时集合做对比探测）；缺省自行连接。
        """
        # 局部 import 避免在未安装 qdrant-client 时导入失败。
        from .qdrant_store import (
            build_point,
            connect,
            ensure_collection,
            upsert_points,
        )

        self._embedder = embedder
        self._config = config
        self._client = client if client is not None else connect(config)
        ensure_collection(self._client, config)
        self._build_point = build_point
        self._upsert = upsert_points
        logger.info("retriever.qdrant.ready collection=%s", config.collection_name)

    @property
    def config(self) -> QdrantConfig:
        return self._config

    def __len__(self) -> int:
        """返回当前集合中的点数量。"""
        info = self._client.get_collection(self._config.collection_name)
        return int(info.points_count or 0)

    def index(self, chunks: list[Chunk]) -> int:
        """把片段向量化并写入 Qdrant；返回写入数量。"""
        if not chunks:
            return 0
        vectors = self._embedder.embed([chunk.text for chunk in chunks])
        expected = self._config.vector_size
        for i, v in enumerate(vectors):
            if len(v) != expected:
                msg = (
                    f"chunk {chunks[i].chunk_id!r} vector dim {len(v)} != "
                    f"collection vector_size {expected}"
                )
                raise ValueError(msg)
        points = [
            self._build_point(
                point_id=chunk.chunk_id,
                vector=vec,
                payload={
                    "chunk_id": chunk.chunk_id,
                    "source": chunk.source,
                    "text": chunk.text,
                    "chunk_index": chunk.metadata.get("chunk_index", -1),
                    "char_start": chunk.metadata.get("char_start", -1),
                },
            )
            for chunk, vec in zip(chunks, vectors, strict=False)
        ]
        return self._upsert(self._client, self._config, points)

    def search(
        self,
        query: str,
        top_k: int = 3,
        source_filter: str | None = None,
    ) -> list[RetrievalResult]:
        """检索与 ``query`` 最相似的 Top-K 个片段。

        Args:
            query: 查询文本。
            top_k: 返回的片段数量。
            source_filter: 可选，按 ``source`` 精确过滤。

        Returns:
            按 ``score`` 降序的 :class:`RetrievalResult` 列表。
        """
        from .qdrant_store import search_points

        if top_k <= 0:
            msg = f"top_k must be > 0, got {top_k}"
            raise ValueError(msg)
        q_vec = self._embedder.embed([query])[0]
        if len(q_vec) != self._config.vector_size:
            msg = (
                f"query vector dim {len(q_vec)} != "
                f"collection vector_size {self._config.vector_size}"
            )
            raise ValueError(msg)
        hits = search_points(
            self._client, self._config, q_vec, top_k=top_k, source_filter=source_filter
        )
        results: list[RetrievalResult] = []
        for hit in hits:
            payload = hit.payload or {}
            results.append(
                RetrievalResult(
                    chunk_id=str(payload.get("chunk_id", hit.id)),
                    source=str(payload.get("source", "")),
                    score=float(hit.score or 0.0),
                    text=str(payload.get("text", "")),
                )
            )
        return results
