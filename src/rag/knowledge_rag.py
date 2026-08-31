"""生产型知识库：多格式导入 + 向量检索编排。

:class:`JwipcKnowledgeRAG` 在 W05 的 ``ingestion`` / ``embeddings`` /
``qdrant_store`` / ``retriever`` 之上做一层编排：

- 导入阶段按文件扩展名分派解析器（Markdown / TXT 走既有切分，
  PDF 走 :mod:`rag.pdf_reader`），统一产出 :class:`Chunk` 并写入 Qdrant；
- 检索阶段复用 :class:`rag.retriever.QdrantRetriever`，只负责召回，
  不调用 LLM（生成在后续阶段实现）。

检索增强为可选项：:func:`build_retriever` 按 ``strategy`` 一键构造检索器
（vector / hybrid / rerank / hybrid+rerank），也可通过 ``retriever`` /
``reranker`` 参数注入自定义实现；两者默认均为 ``None``，此时行为与纯向量
检索完全一致。

本模块不引入新的向量化或存储实现，仅组合既有组件，保持职责分离。
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .embeddings import DEFAULT_DIM, FakeEmbedding, get_embedding
from .hybrid_search import BigramBM25, HybridRetriever, RerankRetriever
from .ingestion import Chunk, build_chunks
from .metadata_filter import MetadataConditions
from .qdrant_store import QdrantConfig
from .rerank import EmbeddingReranker
from .retriever import QdrantRetriever, RetrievalResult

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

    from .rerank import Reranker

logger = logging.getLogger(__name__)

_TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
_PDF_SUFFIXES = {".pdf"}

RETRIEVAL_STRATEGIES = ("vector", "hybrid", "rerank", "hybrid+rerank")
"""检索策略取值：``vector`` 纯向量；``hybrid`` 向量+BM25 RRF；
``rerank`` 粗召回+精排；``hybrid+rerank`` 混合召回后再精排。"""


def build_retriever(
    embedder: FakeEmbedding | object,
    config: QdrantConfig,
    client: object | None = None,
    *,
    strategy: str = "vector",
    coarse_top_k: int = 20,
    top_candidates: int = 30,
) -> QdrantRetriever | HybridRetriever | RerankRetriever:
    """按策略构造检索器，统一出口供脚本与生成链路选择检索方式。

    Args:
        embedder: Embedding 客户端（真实或离线 Fake）。
        config: Qdrant 集合配置。
        client: 可选已建连的 QdrantClient（测试注入用）。
        strategy: ``vector`` / ``hybrid`` / ``rerank`` / ``hybrid+rerank``。
        coarse_top_k: 精排前粗召回条数（仅含精排的策略生效）。
        top_candidates: 混合检索每路候选条数（仅含混合的策略生效）。

    Returns:
        实现 ``index(chunks)`` / ``__len__`` /
        ``search(query, top_k, *, source_filter, metadata)`` 的检索器，
        可直接注入 :class:`JwipcKnowledgeRAG`（``retriever=`` 参数）。
    """
    if strategy not in RETRIEVAL_STRATEGIES:
        msg = f"unknown strategy: {strategy!r} (expected one of {RETRIEVAL_STRATEGIES})"
        raise ValueError(msg)
    vector = QdrantRetriever(embedder, config, client)  # type: ignore[arg-type]
    if strategy == "vector":
        return vector
    if strategy == "hybrid":
        return HybridRetriever(vector, BigramBM25(), top_candidates=top_candidates)
    reranker = EmbeddingReranker(embedder)  # type: ignore[arg-type]
    if strategy == "rerank":
        return RerankRetriever(vector, reranker, coarse_top_k=coarse_top_k)
    return RerankRetriever(
        HybridRetriever(vector, BigramBM25(), top_candidates=top_candidates),
        reranker,
        coarse_top_k=coarse_top_k,
    )


class UnsupportedDocumentError(ValueError):
    """遇到不支持的文件扩展名时抛出。"""


class JwipcKnowledgeRAG:
    """多格式知识库：导入文档并支持向量检索。

    构造时即建立底层 Qdrant 检索器（确保集合存在）。导入的片段会按
    ``chunk_size`` / ``overlap`` 切分后写入同一集合。
    """

    def __init__(
        self,
        embedder: FakeEmbedding | object,
        config: QdrantConfig,
        client: QdrantClient | None = None,
        *,
        chunk_size: int = 400,
        overlap: int = 80,
        retriever: object | None = None,
        reranker: Reranker | None = None,
        coarse_top_k: int = 20,
    ) -> None:
        if chunk_size <= 0:
            msg = f"chunk_size must be > 0, got {chunk_size}"
            raise ValueError(msg)
        if overlap < 0:
            msg = f"overlap must be >= 0, got {overlap}"
            raise ValueError(msg)
        if overlap >= chunk_size:
            msg = f"overlap ({overlap}) must be < chunk_size ({chunk_size})"
            raise ValueError(msg)
        if coarse_top_k <= 0:
            msg = f"coarse_top_k must be > 0, got {coarse_top_k}"
            raise ValueError(msg)
        self._embedder = embedder
        self._config = config
        self._chunk_size = chunk_size
        self._overlap = overlap
        self._reranker = reranker
        self._coarse_top_k = coarse_top_k
        self._retriever = (
            retriever
            if retriever is not None
            else QdrantRetriever(
                embedder,
                config,
                client,  # type: ignore[arg-type]
            )
        )

    @property
    def config(self) -> QdrantConfig:
        return self._config

    @property
    def retriever(self) -> object:
        """当前使用的检索器（策略工厂产出的 vector / hybrid / rerank 包装）。

        供评测等调用方直接复用已喂入数据的检索器（如
        ``rag.evaluate(retriever=rag.retriever, ...)``）。
        """
        return self._retriever

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    @property
    def overlap(self) -> int:
        return self._overlap

    def add_document(self, path: str | Path) -> list[Chunk]:
        """导入单个文档，返回本次产生的片段。

        按扩展名分派解析器；写入 Qdrant 后返回片段列表。空文档返回空列表。
        """
        p = Path(path)
        if not p.exists():
            msg = f"document not found: {p}"
            raise ValueError(msg)
        suffix = p.suffix.lower()
        if suffix in _TEXT_SUFFIXES:
            chunks = [
                Chunk(c.chunk_id, c.source, c.text, {**c.metadata, "file_type": "text"})
                for c in build_chunks([str(p)], chunk_size=self._chunk_size, overlap=self._overlap)
            ]
        elif suffix in _PDF_SUFFIXES:
            from .pdf_reader import parse_pdf

            chunks = parse_pdf(str(p), chunk_size=self._chunk_size, overlap=self._overlap)
        else:
            msg = f"unsupported document type: {suffix!r} (supported: .md/.txt/.pdf)"
            raise UnsupportedDocumentError(msg)
        if chunks:
            self._retriever.index(chunks)
        return chunks

    def add_directory(self, directory: str | Path) -> list[Chunk]:
        """递归导入目录下的全部 ``.md`` / ``.txt`` / ``.pdf`` 文档。

        按文件名排序后逐个导入，返回所有片段的聚合列表。
        """
        d = Path(directory)
        if not d.is_dir():
            msg = f"not a directory: {d}"
            raise ValueError(msg)
        files = sorted(
            {*d.rglob("*.md"), *d.rglob("*.markdown"), *d.rglob("*.txt"), *d.rglob("*.pdf")},
            key=lambda x: x.name,
        )
        all_chunks: list[Chunk] = []
        for f in files:
            all_chunks.extend(self.add_document(f))
        return all_chunks

    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        *,
        source_filter: str | None = None,
        metadata: MetadataConditions | None = None,
    ) -> list[RetrievalResult]:
        """检索与 ``query`` 最相似的 Top-K 片段（仅召回，不生成答案）。

        未配置精排器时等价于 ``QdrantRetriever.search``（支持
        ``source_filter`` / ``metadata`` 过滤）；配置了 ``reranker`` 时
        先粗召回 ``coarse_top_k`` 条再精排取 Top-K。
        """
        if self._reranker is None:
            return self._retriever.search(
                query,
                top_k=top_k,
                source_filter=source_filter,
                metadata=metadata,
            )
        coarse = self._retriever.search(
            query,
            top_k=self._coarse_top_k,
            source_filter=source_filter,
            metadata=metadata,
        )
        return self._reranker.rerank(query, coarse, top_k)

    def count(self) -> int:
        """返回集合中已写入的片段数量。"""
        return len(self._retriever)

    def rebuild(self) -> None:
        """删除并重建当前集合（复用检索器自身的 client）。

        与「先构造后删除」导致的 404 时序问题相反，本方法按「删→建」原子语义
        调用 ``QdrantRetriever.recreate``，并清空下游混合检索的 BM25 索引，
        保证重新导入时不被旧语料污染。本地（``:memory:``）与 Docker 模式行为一致。
        """
        from .hybrid_search import HybridRetriever
        from .retriever import QdrantRetriever

        if isinstance(self._retriever, QdrantRetriever):
            self._retriever.recreate()
        if isinstance(self._retriever, HybridRetriever):
            self._retriever.clear_index()


def build_qdrant_config(collection_name: str, embedder: object) -> QdrantConfig:
    """根据环境变量构造 Qdrant 配置，便于不修改代码切换 local / docker 模式。

    向量维度与 Embedding 输出对齐：``FakeEmbedding`` 取其 ``dim``，真实客户端
    默认使用 :data:`rag.embeddings.DEFAULT_DIM`（mxbai-embed-large 为 1024）。
    """
    vector_size = (
        embedder.dim if isinstance(embedder, FakeEmbedding) else DEFAULT_DIM  # type: ignore[attr-defined]
    )
    return QdrantConfig(
        mode=os.getenv("QDRANT_MODE", "local"),
        path=os.getenv("QDRANT_PATH", ":memory:"),
        host=os.getenv("QDRANT_HOST", "localhost"),
        port=int(os.getenv("QDRANT_PORT", "6333")),
        collection_name=collection_name,
        vector_size=vector_size,
    )


def main(argv: list[str] | None = None) -> int:
    """命令行入口：导入文件或目录到指定 Qdrant 集合。

    用法示例（项目根目录，离线默认使用 FakeEmbedding + 内存 Qdrant）::

        uv run python scripts/rag_week6_ingest.py --ingest data/raw --collection jwipc_knowledge

    接入真实 Embedding / Docker Qdrant 时通过 ``.env`` 配置
    ``EMBEDDING_API_BASE_URL`` / ``EMBEDDING_MODEL_NAME`` / ``QDRANT_*`` 即可。
    """
    parser = argparse.ArgumentParser(description="JwipcKnowledgeRAG 文档导入工具")
    parser.add_argument("--ingest", required=True, help="待导入的文件或目录路径")
    parser.add_argument("--collection", default="jwipc_knowledge", help="Qdrant 集合名")
    parser.add_argument("--chunk-size", type=int, default=400, help="单片段目标字符数")
    parser.add_argument("--overlap", type=int, default=80, help="相邻片段重叠字符数")
    parser.add_argument("--rebuild", action="store_true", help="导入前删除并重建集合（清空旧数据）")
    args = parser.parse_args(argv)

    embedder = get_embedding()
    cfg = build_qdrant_config(args.collection, embedder)
    rag = JwipcKnowledgeRAG(embedder, cfg, chunk_size=args.chunk_size, overlap=args.overlap)
    if args.rebuild:
        rag.rebuild()
    target = Path(args.ingest)
    chunks = rag.add_directory(target) if target.is_dir() else rag.add_document(target)
    print(
        f"ingested {len(chunks)} chunks into collection={cfg.collection_name}; total={rag.count()}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
