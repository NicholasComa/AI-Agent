"""BM25 稀疏检索：不依赖分词器的字符 Bigram 实现。

语料为短中文文档，不引入分词器；中文按相邻汉字二元组（bigram）
切分，ASCII 字母数字按整词切分，与向量检索形成互补信号。
"""

from __future__ import annotations

import re

from rank_bm25 import BM25Okapi

from rag.ingestion import Chunk
from rag.retriever import RetrievalResult

_ASCII_WORD = re.compile(r"[a-zA-Z0-9_]+")


def tokenize(text: str) -> list[str]:
    """把文本切为 token 列表：ASCII 整词 + 中文汉字 bigram。"""
    tokens = [word.lower() for word in _ASCII_WORD.findall(text)]
    cjk = "".join(c for c in text if "\u4e00" <= c <= "\u9fff")
    tokens.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))
    return tokens


class BigramBM25:
    """基于字符 Bigram 的 BM25 检索器（内存索引）。"""

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._index: BM25Okapi | None = None

    def index(self, chunks: list[Chunk]) -> None:
        """重建 BM25 索引（与向量索引共用同一批 ``Chunk``，chunk_id 对齐）。"""
        self._chunks = list(chunks)
        corpus = [tokenize(chunk.text) for chunk in self._chunks]
        self._index = BM25Okapi(corpus) if corpus else None

    def search(self, query: str, top_k: int = 3) -> list[RetrievalResult]:
        """按 BM25 分数返回 Top-K 结果。"""
        if top_k <= 0:
            msg = f"top_k must be > 0, got {top_k}"
            raise ValueError(msg)
        if self._index is None:
            msg = "BigramBM25 has no index; call index(chunks) first"
            raise RuntimeError(msg)
        scores = self._index.get_scores(tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        results: list[RetrievalResult] = []
        for i in order:
            if len(results) >= top_k:
                break
            chunk = self._chunks[i]
            results.append(
                RetrievalResult(
                    chunk_id=chunk.chunk_id,
                    source=chunk.source,
                    score=float(scores[i]),
                    text=chunk.text,
                )
            )
        return results
