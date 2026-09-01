"""JwipcKnowledgeRAG 单元测试（离线：FakeEmbedding + 内存 Qdrant :memory:）。

覆盖：多格式导入（MD/TXT/PDF）、不支持类型与缺失文件的异常、目录递归聚合、
检索字段完整性、chunk_size/overlap 参数校验、PDF 元数据的正确性，以及
``rebuild()`` 的集合重建与 BM25 索引清理。
PDF 测试夹具在 tmp_path 内用最小合法 PDF 动态生成，无需外部 PDF 文件。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from rag.embeddings import FakeEmbedding
from rag.hybrid_search import BigramBM25, HybridRetriever
from rag.knowledge_rag import JwipcKnowledgeRAG, UnsupportedDocumentError
from rag.qdrant_store import QdrantConfig
from rag.retriever import QdrantRetriever


def _build_pdf(page_texts: list[str]) -> bytes:
    """生成一个最小合法 PDF（含正确 xref），供 pypdf 提取文本。"""
    objs: list[bytes] = []
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(page_texts)))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_texts)} >>".encode())
    font_num = 3 + 2 * len(page_texts)
    for i, txt in enumerate(page_texts):
        content = f"BT /F1 12 Tf 72 720 Td ({txt}) Tj ET".encode()
        page = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> "
            f"/Contents {4 + 2 * i} 0 R >>"
        ).encode()
        objs.append(page)
        objs.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, o in enumerate(objs, start=1):
        offsets.append(buf.tell())
        buf.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref_pos = buf.tell()
    n = len(objs) + 1
    buf.write(f"xref\n0 {n}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode())
    return buf.getvalue()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_pdf(path: Path, page_texts: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_build_pdf(page_texts))
    return path


def _make_cfg(dim: int = 8, collection: str = "wk6_rag") -> QdrantConfig:
    return QdrantConfig(mode="local", path=":memory:", collection_name=collection, vector_size=dim)


def _rag(dim: int = 8, collection: str = "wk6_rag") -> JwipcKnowledgeRAG:
    return JwipcKnowledgeRAG(FakeEmbedding(dim=dim), _make_cfg(dim, collection))


def test_add_document_markdown_and_txt(tmp_path: Path):
    """MD / TXT 导入后写入 Qdrant，count 与片段数一致且带 file_type 元数据。"""
    rag = _rag()
    a = _write(tmp_path / "a.md", "hello world content")
    b = _write(tmp_path / "b.txt", "foo bar baz")
    ca = rag.add_document(a)
    cb = rag.add_document(b)
    assert ca and cb
    assert ca[0].metadata["file_type"] == "text"
    assert ca[0].source == "a.md"
    assert rag.count() == len(ca) + len(cb)


def test_add_document_pdf_extracts_pages(tmp_path: Path):
    """PDF 导入按页切分，片段带 file_type=pdf 与 page 元数据，source 为文件名。"""
    rag = _rag()
    pdf = _write_pdf(
        tmp_path / "doc.pdf",
        [
            "page one text body with enough characters to pass the default min chars filter",
            "page two text body with enough characters to pass the default min chars filter",
        ],
    )
    chunks = rag.add_document(pdf)
    assert len(chunks) >= 2
    ids = {c.chunk_id for c in chunks}
    assert len(ids) == len(chunks), "chunk_id 应唯一"
    for c in chunks:
        assert c.metadata["file_type"] == "pdf"
        assert "page" in c.metadata
        assert c.source == "doc.pdf"
    assert rag.count() == len(chunks)


def test_add_document_pdf_page_metadata_and_chunk_id(tmp_path: Path):
    """PDF 片段的 page 元数据与 chunk_id 能对应到实际页码。"""
    rag = _rag()
    pdf = _write_pdf(
        tmp_path / "pages.pdf",
        [
            "first page body with enough characters to pass the default min chars filter",
            "second page body with enough characters to pass the default min chars filter",
        ],
    )
    chunks = rag.add_document(pdf)
    pages = sorted(c.metadata["page"] for c in chunks)
    assert pages[0] == 0
    assert pages[1] == 1
    assert any("#p0" in c.chunk_id for c in chunks)
    assert any("#p1" in c.chunk_id for c in chunks)


def test_add_document_pdf_skips_empty_page(tmp_path: Path):
    """无文本可提取的页面被跳过，不产生片段。"""
    rag = _rag()
    pdf = _write_pdf(
        tmp_path / "emptyp.pdf",
        [
            "",
            "only this page has text body with enough characters to pass the default min chars filter",
        ],
    )
    chunks = rag.add_document(pdf)
    assert len(chunks) == 1
    assert chunks[0].metadata["page"] == 1


def test_add_document_unsupported_suffix_raises(tmp_path: Path):
    """不支持的扩展名（如 .docx）抛出 UnsupportedDocumentError。"""
    rag = _rag()
    bad = _write(tmp_path / "c.docx", "x")
    with pytest.raises(UnsupportedDocumentError):
        rag.add_document(bad)


def test_add_document_missing_file_raises(tmp_path: Path):
    """文件不存在时抛出 ValueError。"""
    rag = _rag()
    with pytest.raises(ValueError):
        rag.add_document(tmp_path / "nope.md")


def test_add_directory_recursive_aggregates(tmp_path: Path):
    """递归目录导入聚合所有 MD / PDF 片段，PDF 片段带有 pdf 元数据。"""
    root = tmp_path / "dir"
    _write(root / "sub" / "a.md", "markdown content here")
    _write_pdf(
        root / "b.pdf",
        [
            "pdf page alpha body with enough characters to pass the default min chars filter",
            "pdf page beta body with enough characters to pass the default min chars filter",
        ],
    )
    rag = _rag(collection="wk6_dir")
    chunks = rag.add_directory(root)
    sources = {c.source for c in chunks}
    assert "a.md" in sources
    assert "b.pdf" in sources
    assert any(c.metadata.get("file_type") == "pdf" for c in chunks)
    assert rag.count() == len(chunks)


def test_retrieve_returns_required_fields(tmp_path: Path):
    """检索结果包含 chunk_id / source / score / text，且 score 为 float。"""
    rag = _rag()
    _write(tmp_path / "r.md", "向量数据库用于存储嵌入向量。")
    rag.add_document(tmp_path / "r.md")
    results = rag.retrieve("向量数据库", top_k=2)
    assert results
    for r in results:
        assert r.chunk_id
        assert r.source
        assert isinstance(r.score, float)
        assert r.text


def test_chunk_size_overlap_validation():
    """chunk_size<=0 或 overlap>=chunk_size 时构造即抛 ValueError。"""
    emb = FakeEmbedding(dim=8)
    cfg = _make_cfg(8)
    with pytest.raises(ValueError):
        JwipcKnowledgeRAG(emb, cfg, chunk_size=0)
    with pytest.raises(ValueError):
        JwipcKnowledgeRAG(emb, cfg, chunk_size=100, overlap=100)


def test_add_document_pdf_drops_short_page_fragments(tmp_path: Path):
    """低于默认 min_chars 的页面碎片（封面页 / 图表标题）被丢弃。"""
    rag = _rag(collection="wk6_shortpdf")
    body = "body text with enough characters to survive the default min chars filter"
    pdf = _write_pdf(tmp_path / "short.pdf", ["封面", body])
    chunks = rag.add_document(pdf)
    assert len(chunks) == 1
    assert chunks[0].metadata["page"] == 1
    assert chunks[0].text.strip() == body


def test_rebuild_empties_collection_and_allows_reingest(tmp_path: Path):
    """rebuild() 后集合清空且可立即重新写入（不触发集合不存在的 404）。"""
    rag = _rag(collection="wk6_rebuild")
    doc = _write(tmp_path / "a.md", "hello world content here for rebuild test")
    rag.add_document(doc)
    assert rag.count() == 1

    rag.rebuild()
    assert rag.count() == 0

    rag.add_document(doc)
    assert rag.count() == 1


def test_rebuild_clears_bm25_index(tmp_path: Path):
    """rebuild() 同时清空混合检索的 BM25 索引，避免旧语料残留在稀疏路。"""
    emb = FakeEmbedding(dim=8)
    cfg = _make_cfg(8, "wk6_rebuild_hybrid")
    bm25 = BigramBM25()
    hybrid = HybridRetriever(QdrantRetriever(emb, cfg), bm25)
    rag = JwipcKnowledgeRAG(emb, cfg, retriever=hybrid)
    rag.add_document(_write(tmp_path / "b.md", "vector database content for bm25 clear test"))
    assert len(bm25) == 1

    rag.rebuild()

    assert len(bm25) == 0
