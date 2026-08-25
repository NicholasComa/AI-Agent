"""生产型知识库：多格式导入 + 向量检索编排。

:class:`JwipcKnowledgeRAG` 在 W05 的 ``ingestion`` / ``embeddings`` /
``qdrant_store`` / ``retriever`` 之上做一层编排：

- 导入阶段按文件扩展名分派解析器（Markdown / TXT 走既有切分，
  PDF 走 :mod:`rag.pdf_reader`），统一产出 :class:`Chunk` 并写入 Qdrant；
- 检索阶段复用 :class:`rag.retriever.QdrantRetriever`，只负责召回，
  不调用 LLM（生成在后续阶段实现）。

本模块不引入新的向量化或存储实现，仅组合既有组件，保持职责分离。
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .embeddings import DEFAULT_DIM, FakeEmbedding, get_embedding
from .ingestion import Chunk, build_chunks
from .qdrant_store import QdrantConfig, connect
from .retriever import QdrantRetriever, RetrievalResult

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

logger = logging.getLogger(__name__)

_TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
_PDF_SUFFIXES = {".pdf"}


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
        self._embedder = embedder
        self._config = config
        self._chunk_size = chunk_size
        self._overlap = overlap
        self._retriever = QdrantRetriever(embedder, config, client)  # type: ignore[arg-type]

    @property
    def config(self) -> QdrantConfig:
        return self._config

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

    def retrieve(self, query: str, top_k: int = 3) -> list[RetrievalResult]:
        """检索与 ``query`` 最相似的 Top-K 片段（仅召回，不生成答案）。"""
        return self._retriever.search(query, top_k=top_k)

    def count(self) -> int:
        """返回集合中已写入的片段数量。"""
        return len(self._retriever)


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
    if args.rebuild:
        client = connect(cfg)
        if client.collection_exists(cfg.collection_name):
            client.delete_collection(cfg.collection_name)
            logger.info("qdrant.collection.deleted name=%s", cfg.collection_name)

    rag = JwipcKnowledgeRAG(embedder, cfg, chunk_size=args.chunk_size, overlap=args.overlap)
    target = Path(args.ingest)
    chunks = rag.add_directory(target) if target.is_dir() else rag.add_document(target)
    print(
        f"ingested {len(chunks)} chunks into collection={cfg.collection_name}; total={rag.count()}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
