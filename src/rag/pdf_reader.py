"""PDF 文档解析。

把 PDF 每个页面的正文提取为纯文本，再按窗口切分为片段（:class:`Chunk`），
与 :mod:`rag.ingestion` 的 Markdown / TXT 切分保持同一结构，便于统一写入
Qdrant 与后续检索。

PDF 解析依赖 ``pypdf``（``uv add pypdf``）。解析失败时抛出明确的运行时错误，
而不是静默返回空结果。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .ingestion import Chunk, chunk_text

# pypdf 的导入放在函数内，避免在未安装时导入本模块即报错。
_PYPDF_ERROR = "pypdf is required for PDF ingestion (run: uv add pypdf)"


def parse_pdf(path: str | Path, *, chunk_size: int = 400, overlap: int = 80) -> list[Chunk]:
    """解析一个 PDF 文件，返回按页面切分后的片段列表。

    每个页面的文本先经 :func:`rag.ingestion.chunk_text` 切分（受 ``chunk_size`` /
    ``overlap`` 控制），再为每个片段补充 ``file_type="pdf"`` 与 ``page`` 元数据，
    供后续溯源与过滤。

    Args:
        path: PDF 文件路径。
        chunk_size: 单片段目标字符数。
        overlap: 相邻片段重叠字符数。

    Returns:
        :class:`Chunk` 列表；无文本可提取的页面会被跳过。
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖缺失由安装步骤保证
        raise RuntimeError(_PYPDF_ERROR) from exc

    pdf_path = Path(path)
    reader = PdfReader(str(pdf_path))
    source = pdf_path.name
    chunks: list[Chunk] = []
    for page_idx, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        page_source = f"{source}#p{page_idx}"
        for base in chunk_text(text, page_source, chunk_size=chunk_size, overlap=overlap):
            merged: dict[str, Any] = {**base.metadata, "file_type": "pdf", "page": page_idx}
            chunks.append(
                Chunk(
                    chunk_id=base.chunk_id,
                    source=source,
                    text=base.text,
                    metadata=merged,
                )
            )
    return chunks
