"""文档导入与切分。

RAG 管道的第一个环节：把原始资料（Markdown / TXT）读成纯文本，再按
固定窗口切分为若干片段（Chunk），并为每个片段附加元数据（Metadata）以便后续
溯源与过滤。

本模块只做「解析 + 切分」，**不调用任何 LLM**，也不负责向量化 —— 向量化在
:mod:`rag.embeddings`，检索在 :mod:`rag.retriever`。三者职责分离，便于检索与
生成的环节各自独立演进。

切分策略
--------

- ``chunk_size``：每个片段的目标字符数（上限）。
- ``overlap``：相邻片段重叠的字符数，用于避免跨片段边界的信息被切断。

满足 ``0 <= overlap < chunk_size``，否则抛出 :class:`ValueError`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 支持的纯文本扩展名；其它类型（如 .pdf）暂未接入，明确报错而不是静默跳过。
_TEXT_SUFFIXES = {".md", ".txt"}


@dataclass(frozen=True)
class Chunk:
    """一个文档片段。

    Attributes:
        chunk_id: 全局唯一的片段标识（``"{source}#{index}"``），供检索结果溯源。
        source: 片段所属的源文档名。
        text: 片段正文。
        metadata: 附加元数据（``chunk_index`` / ``char_start`` 等），用于过滤。
    """

    chunk_id: str
    source: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def load_documents(paths: list[str | Path]) -> list[tuple[str, str]]:
    """读取一组源文档，返回 ``(source, text)`` 列表。

    Args:
        paths: 源文档路径列表（支持 ``.md`` / ``.txt``）。

    Returns:
        ``(source, text)`` 列表，``source`` 为文件名（``Path.name``）。

    Raises:
        ValueError: 路径不存在，或扩展名不受支持（例如 ``.pdf`` 尚未接入）。
    """
    documents: list[tuple[str, str]] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            msg = f"document not found: {path}"
            raise ValueError(msg)
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            msg = f"unsupported document type: {path.suffix!r} (only .md/.txt supported)"
            raise ValueError(msg)
        text = path.read_text(encoding="utf-8")
        documents.append((path.name, text))
    return documents


def chunk_text(text: str, source: str, *, chunk_size: int = 300, overlap: int = 60) -> list[Chunk]:
    """把单篇文本切分为若干片段。

    Args:
        text: 待切分的纯文本。
        source: 源文档名，用于生成 ``chunk_id`` 与 ``metadata``。
        chunk_size: 每个片段的目标字符数。
        overlap: 相邻片段重叠字符数。

    Returns:
        :class:`Chunk` 列表；空文本返回空列表。

    Raises:
        ValueError: ``chunk_size <= 0``、``overlap < 0`` 或 ``overlap >= chunk_size``。
    """
    if chunk_size <= 0:
        msg = f"chunk_size must be > 0, got {chunk_size}"
        raise ValueError(msg)
    if overlap < 0:
        msg = f"overlap must be >= 0, got {overlap}"
        raise ValueError(msg)
    if overlap >= chunk_size:
        msg = f"overlap ({overlap}) must be < chunk_size ({chunk_size})"
        raise ValueError(msg)

    step = chunk_size - overlap
    chunks: list[Chunk] = []
    start = 0
    idx = 0
    while start < len(text):
        piece = text[start : start + chunk_size]
        if not piece:
            break
        chunks.append(
            Chunk(
                chunk_id=f"{source}#{idx}",
                source=source,
                text=piece,
                metadata={"chunk_index": idx, "char_start": start},
            )
        )
        start += step
        idx += 1
    return chunks


def build_chunks(
    paths: list[str | Path], *, chunk_size: int = 300, overlap: int = 60
) -> list[Chunk]:
    """读取并切分一组源文档，返回全部片段。

    Args:
        paths: 源文档路径列表。
        chunk_size: 每个片段的目标字符数。
        overlap: 相邻片段重叠字符数。

    Returns:
        所有源文档切分出的 :class:`Chunk` 列表（``chunk_id`` 在整批内唯一）。
    """
    chunks: list[Chunk] = []
    for source, text in load_documents(paths):
        chunks.extend(chunk_text(text, source, chunk_size=chunk_size, overlap=overlap))
    return chunks
