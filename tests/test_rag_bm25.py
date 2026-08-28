"""rag.bm25 单元测试：字符 Bigram BM25 稀疏检索。"""

from __future__ import annotations

import pytest

from rag.bm25 import BigramBM25, tokenize
from rag.ingestion import Chunk


def _chunks() -> list[Chunk]:
    return [
        Chunk(
            chunk_id="c1",
            source="a.md",
            text="RAG 是检索增强生成。",
            metadata={"chunk_index": 0, "char_start": 0},
        ),
        Chunk(
            chunk_id="c2",
            source="b.md",
            text="向量数据库用于存储嵌入向量。",
            metadata={"chunk_index": 0, "char_start": 0},
        ),
        Chunk(
            chunk_id="c3",
            source="c.md",
            text="今天天气很好。",
            metadata={"chunk_index": 0, "char_start": 0},
        ),
    ]


def test_tokenize_splits_ascii_words_and_cjk_bigrams() -> None:
    """正常：ASCII 按整词、中文按相邻汉字 bigram 切分。"""
    tokens = tokenize("RAG 检索增强")
    assert "rag" in tokens
    assert "检索" in tokens
    assert "索增" in tokens


def test_bm25_ranks_exact_token_match_first() -> None:
    """正常：与语料 token 精确匹配的片段排最前。"""
    bm25 = BigramBM25()
    bm25.index(_chunks())
    results = bm25.search("检索增强", top_k=1)
    assert results[0].chunk_id == "c1"


def test_bm25_raises_before_index_and_bad_topk() -> None:
    """异常：未建索引调用 search 抛 RuntimeError；top_k<=0 抛 ValueError。"""
    bm25 = BigramBM25()
    with pytest.raises(RuntimeError, match="has no index"):
        bm25.search("查询", top_k=1)
    bm25.index(_chunks())
    with pytest.raises(ValueError, match="top_k must be > 0"):
        bm25.search("查询", top_k=0)
