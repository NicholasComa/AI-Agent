"""ingestion 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.ingestion import Chunk, build_chunks, chunk_text, load_documents


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_chunk_text_returns_required_fields():
    chunks = chunk_text("abcdefghij", "doc.md", chunk_size=4, overlap=2)
    assert chunks, "应至少产生一个片段"
    for c in chunks:
        assert c.chunk_id == f"doc.md#{c.metadata['chunk_index']}"
        assert c.source == "doc.md"
        assert isinstance(c.text, str)
        assert c.metadata["char_start"] >= 0


def test_chunk_text_overlap_between_neighbors():
    text = "0123456789abcdefghij"
    chunk_size = 6
    overlap = 2
    chunks = chunk_text(text, "doc.md", chunk_size=chunk_size, overlap=overlap)
    # 除最后一个余数片段外，相邻「完整」片段应共享 overlap 个字符。
    for a, b in zip(chunks, chunks[1:], strict=False):
        if len(b.text) == chunk_size:
            assert a.text[-overlap:] == b.text[:overlap]


def test_chunk_text_empty_text_returns_empty():
    assert chunk_text("", "empty.md", chunk_size=4, overlap=2) == []


@pytest.mark.parametrize(
    ("chunk_size", "overlap"),
    [(0, 0), (-1, 0), (4, -1), (4, 4), (4, 5)],
)
def test_chunk_text_invalid_params_raise(chunk_size, overlap):
    with pytest.raises(ValueError):
        chunk_text("abc", "doc.md", chunk_size=chunk_size, overlap=overlap)


def test_load_documents_reads_md_and_txt(tmp_path):
    md = _write(tmp_path / "a.md", "hello")
    txt = _write(tmp_path / "b.txt", "world")
    docs = load_documents([md, txt])
    assert docs == [("a.md", "hello"), ("b.txt", "world")]


def test_load_documents_missing_file_raises(tmp_path):
    with pytest.raises(ValueError):
        load_documents([tmp_path / "nope.md"])


def test_load_documents_unsupported_suffix_raises(tmp_path):
    pdf = _write(tmp_path / "c.pdf", "not text")
    with pytest.raises(ValueError):
        load_documents([pdf])


def test_build_chunks_tmp_generates_expected_count(tmp_path):
    text = "x" * 100  # chunk_size=30, overlap=0 -> 4 片段（30+30+30+10）
    path = _write(tmp_path / "doc.md", text)
    chunks = build_chunks([path], chunk_size=30, overlap=0)
    assert len(chunks) == 4


def test_build_chunks_real_training_data_at_least_30():
    """data/raw 的公开训练资料切分后至少 30 个片段。"""
    repo_root = Path(__file__).resolve().parent.parent
    raw_dir = repo_root / "data" / "raw"
    paths = sorted(raw_dir.glob("*.md"))
    assert paths, "data/raw 下应存在 .md 训练资料"
    chunks = build_chunks([str(p) for p in paths], chunk_size=300, overlap=60)
    assert len(chunks) >= 30, f"片段数 {len(chunks)} < 30"
    assert isinstance(chunks[0], Chunk)
