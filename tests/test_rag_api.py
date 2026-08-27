"""``POST /rag/query`` FastAPI 端点测试（ASGITransport，不联网）。

用 FakeEmbedding + 内存 Qdrant + 假 LLM（FakeChat）覆盖：

- 有答案：返回 has_answer=true，引用命中召回片段（chunk_id 白名单）；
- 召回侧拒答（低分）：不调用 LLM，返回「资料中未找到」；
- 非法引用：LLM 引用不在召回集合内时降级拒答；
- 请求校验：空 question / top_k 越界返回 422。

隔离约定沿用 ``test_rag_generator.py``：命中路径显式 ``min_score=0.0``
与阈值解耦，阈值行为用「无共享 token 的查询」单独验证。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from rag.embeddings import FakeEmbedding
from rag.generator import NO_ANSWER_TEXT
from rag.knowledge_rag import JwipcKnowledgeRAG
from rag.qdrant_store import QdrantConfig
from rag_api import create_rag_app

DIM = 1024
DOC_ZH = "Qdrant 是向量数据库，支持语义检索。"
QUERY_HIT = "Qdrant 是什么数据库？"


class FakeChat:
    """可注入的假 LLM：返回预设文本（或按消息动态生成），并记录调用。"""

    def __init__(self, payload: str | Callable[[list[dict[str, str]]], str]) -> None:
        self._payload = payload
        self.calls: list[list[dict[str, str]]] = []

    async def __call__(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        if callable(self._payload):
            return self._payload(messages)
        return self._payload

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _make_rag(embedder: FakeEmbedding, tmp_path) -> JwipcKnowledgeRAG:
    cfg = QdrantConfig(
        mode="local",
        path=":memory:",
        collection_name="api_test",
        vector_size=embedder.dim,
    )
    rag = JwipcKnowledgeRAG(embedder, cfg)
    p = tmp_path / "qdrant_intro.md"
    p.write_text(DOC_ZH, encoding="utf-8")
    rag.add_document(p)
    return rag


def _hit_payload(chunk_id: str) -> str:
    payload = {
        "answer": "Qdrant 是一个向量数据库。",
        "has_answer": True,
        "citations": [{"source": "qdrant_intro.md", "chunk_id": chunk_id, "quote": DOC_ZH}],
        "confidence": 0.9,
    }
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture
def embedder() -> FakeEmbedding:
    return FakeEmbedding(dim=DIM)


@pytest.fixture
def rag(embedder: FakeEmbedding, tmp_path) -> JwipcKnowledgeRAG:
    return _make_rag(embedder, tmp_path)


@pytest.fixture
async def api(
    rag: JwipcKnowledgeRAG,
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeChat]]:
    """注入 FakeChat 与同一内存知识库的 RAG API 测试客户端。"""
    top = rag.retrieve(QUERY_HIT, top_k=1)
    assert top, "fixture 语料应能召回结果"
    chunk_id = top[0].chunk_id
    fake_chat = FakeChat(_hit_payload(chunk_id))
    app = create_rag_app(rag_factory=lambda: rag, chat_factory=lambda: fake_chat)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as ac,
    ):
        yield ac, fake_chat


async def test_rag_query_hit(api: tuple[httpx.AsyncClient, FakeChat]) -> None:
    """知识库内问题 → has_answer=true，引用指向召回片段。"""
    client, fake_chat = api
    resp = await client.post(
        "/rag/query",
        json={"question": QUERY_HIT, "min_score": 0.0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_answer"] is True
    assert body["answer"] == "Qdrant 是一个向量数据库。"
    assert body["rejected_reason"] is None
    assert len(body["citations"]) == 1
    assert body["citations"][0]["source"] == "qdrant_intro.md"
    assert body["citations"][0]["quote"] == DOC_ZH
    assert fake_chat.call_count == 1


async def test_rag_query_low_score_rejects_without_llm(
    api: tuple[httpx.AsyncClient, FakeChat],
) -> None:
    """库外问题 → 召回侧拒答（low_score），且不调用 LLM。"""
    client, fake_chat = api
    fake_chat.calls.clear()

    resp = await client.post("/rag/query", json={"question": "今天天气怎么样？"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_answer"] is False
    assert body["answer"] == NO_ANSWER_TEXT
    assert body["rejected_reason"] == "low_score"
    assert fake_chat.call_count == 0


async def test_rag_query_invalid_citations_reject(
    api: tuple[httpx.AsyncClient, FakeChat],
) -> None:
    """LLM 引用不在召回集合内 → 降级拒答（invalid_citations）。"""
    client, fake_chat = api
    fake_chat._payload = json.dumps(
        {
            "answer": "编造的答案",
            "has_answer": True,
            "citations": [{"source": "xxx.md", "chunk_id": "xxx.md#0", "quote": "不存在"}],
            "confidence": 0.9,
        },
        ensure_ascii=False,
    )

    resp = await client.post(
        "/rag/query",
        json={"question": QUERY_HIT, "min_score": 0.0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_answer"] is False
    assert body["answer"] == NO_ANSWER_TEXT
    assert body["rejected_reason"] == "invalid_citations"


async def test_rag_query_empty_question_422(
    api: tuple[httpx.AsyncClient, FakeChat],
) -> None:
    """question 为空 → 422。"""
    client, _ = api
    resp = await client.post("/rag/query", json={"question": ""})
    assert resp.status_code == 422
    assert "question" in resp.text


async def test_rag_query_invalid_top_k_422(
    api: tuple[httpx.AsyncClient, FakeChat],
) -> None:
    """top_k 越界（0）→ 422。"""
    client, _ = api
    resp = await client.post("/rag/query", json={"question": QUERY_HIT, "top_k": 0})
    assert resp.status_code == 422
    assert "top_k" in resp.text
