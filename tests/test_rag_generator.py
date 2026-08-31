"""JwipcKnowledgeRAG 检索增强生成（引用 + 拒答）离线测试。

用 FakeEmbedding + 内存 Qdrant + 假 LLM（FakeChat）覆盖生成链路的
全部分支：有答案带引用、低分/空库拒答不调 LLM、LLM 侧拒答、非法引用
过滤与全非法降级、解析失败降级、LLM 异常降级、Prompt 组装与 Schema 严格性。

测试隔离约定：
- 走 LLM 路径的逻辑用例显式传 ``min_score=0.0``，与阈值行为解耦；
- 拒答阈值行为由「英文库 + 中文查询」用例单独验证（无共享 token，
  余弦分数≈0，稳定低于默认 0.3）。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from rag.embeddings import FakeEmbedding
from rag.generator import (
    NO_ANSWER_TEXT,
    Citation,
    RagAnswer,
    RagGenerator,
    build_rag_messages,
)
from rag.knowledge_rag import JwipcKnowledgeRAG
from rag.qdrant_store import QdrantConfig
from rag.retriever import RetrievalResult

# dim=1024 与默认 Embedding 维度一致；高维使无关 token 桶碰撞噪声趋近于 0，
# 保证"无共享 token 的查询分数≈0"这一测试前提稳定成立。
DIM = 1024
DOC_ZH = "Qdrant 是向量数据库，支持语义检索。"
DOC_EN = (
    "Qdrant is an open source vector database that supports payload filtering "
    "and hybrid search for retrieval augmented generation applications."
)
QUERY_HIT = "Qdrant 是什么数据库？"  # 与 DOC_ZH 共享 5 个 token，分数显著高于 0.3


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


def _make_rag(
    embedder: FakeEmbedding,
    tmp_path,
    docs: list[tuple[str, str]],
) -> JwipcKnowledgeRAG:
    cfg = QdrantConfig(
        mode="local",
        path=":memory:",
        collection_name="gen_test",
        vector_size=embedder.dim,
    )
    rag = JwipcKnowledgeRAG(embedder, cfg)
    for name, text in docs:
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        rag.add_document(p)
    return rag


@pytest.fixture
def embedder() -> FakeEmbedding:
    return FakeEmbedding(dim=DIM)


@pytest.fixture
def rag(embedder: FakeEmbedding, tmp_path) -> JwipcKnowledgeRAG:
    return _make_rag(embedder, tmp_path, [("qdrant_intro.md", DOC_ZH)])


@pytest.fixture
def empty_rag(embedder: FakeEmbedding) -> JwipcKnowledgeRAG:
    cfg = QdrantConfig(
        mode="local",
        path=":memory:",
        collection_name="gen_empty",
        vector_size=embedder.dim,
    )
    return JwipcKnowledgeRAG(embedder, cfg)


# ---------------------------------------------------------------------------
# 有答案路径（min_score=0.0，隔离阈值；另以 sanity 断言验证默认阈值可命中）
# ---------------------------------------------------------------------------


def test_retrieval_score_above_default_threshold(rag: JwipcKnowledgeRAG) -> None:
    """fixture 语料在默认 0.3 阈值下应能命中（保证集成行为成立）。"""
    top = rag.retrieve(QUERY_HIT, top_k=1)
    assert top
    assert top[0].score >= 0.3, f"fixture score={top[0].score:.3f} 低于默认阈值"


async def test_answer_with_citations(rag: JwipcKnowledgeRAG) -> None:
    top = rag.retrieve(QUERY_HIT, top_k=3)
    assert top, "fixture 语料应能召回结果"
    chunk_id = top[0].chunk_id
    payload = {
        "answer": "Qdrant 是一个向量数据库。",
        "has_answer": True,
        "citations": [{"source": "qdrant_intro.md", "chunk_id": chunk_id, "quote": DOC_ZH}],
        "confidence": 0.9,
    }
    # 用 markdown 代码块包裹，验证宽容解析
    chat = FakeChat("```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```")
    gen = RagGenerator(rag, chat, min_score=0.0)

    ans = await gen.answer(QUERY_HIT)

    assert ans.has_answer is True
    assert ans.answer == payload["answer"]
    assert ans.rejected_reason is None
    assert len(ans.citations) == 1
    assert ans.citations[0].source == "qdrant_intro.md"
    assert ans.citations[0].chunk_id == chunk_id
    assert ans.citations[0].quote == DOC_ZH
    assert ans.confidence == pytest.approx(0.9)
    assert chat.call_count == 1


# ---------------------------------------------------------------------------
# 召回侧拒答（不调 LLM）
# ---------------------------------------------------------------------------


async def test_reject_low_score_skips_llm(embedder: FakeEmbedding, tmp_path) -> None:
    rag = _make_rag(embedder, tmp_path, [("en.md", DOC_EN)])
    chat = FakeChat("should not be called")

    ans = await RagGenerator(rag, chat).answer("你叫什么名字？")

    assert ans.has_answer is False
    assert ans.answer == NO_ANSWER_TEXT
    assert ans.rejected_reason == "low_score"
    assert chat.call_count == 0


async def test_reject_no_hit_skips_llm(empty_rag: JwipcKnowledgeRAG) -> None:
    chat = FakeChat("should not be called")

    ans = await RagGenerator(empty_rag, chat).answer("任何问题")

    assert ans.rejected_reason == "no_hit"
    assert ans.answer == NO_ANSWER_TEXT
    assert chat.call_count == 0


# ---------------------------------------------------------------------------
# LLM 侧拒答与降级
# ---------------------------------------------------------------------------


async def test_reject_when_llm_says_no(rag: JwipcKnowledgeRAG) -> None:
    payload = {"answer": "", "has_answer": False, "citations": [], "confidence": 0.05}
    gen = RagGenerator(rag, FakeChat(json.dumps(payload)), min_score=0.0)

    ans = await gen.answer(QUERY_HIT)

    assert ans.has_answer is False
    assert ans.rejected_reason == "llm_no_answer"
    assert ans.answer == NO_ANSWER_TEXT


async def test_no_answer_retries_with_double_topk(embedder: FakeEmbedding, tmp_path) -> None:
    """拒答重试：LLM 首判无答案时，用 top_k×2 重新召回再问一次。"""
    rag = _make_rag(
        embedder,
        tmp_path,
        [("a.md", DOC_ZH), ("b.md", DOC_ZH + " 补充第二段内容。"), ("c.md", DOC_ZH + " 第三段内容。")],
    )
    no_payload = {"answer": "", "has_answer": False, "citations": [], "confidence": 0.05}
    ref_counts: list[int] = []

    def _payload(messages: list[dict[str, str]]) -> str:
        ref_counts.append(messages[1]["content"].count("chunk_id="))
        return json.dumps(no_payload)

    chat = FakeChat(_payload)
    gen = RagGenerator(rag, chat, top_k=1, min_score=0.0)

    ans = await gen.answer(QUERY_HIT)

    assert ans.rejected_reason == "llm_no_answer"
    assert chat.call_count == 2
    assert ref_counts == [1, 2], f"第二次应带 top_k×2 的参考资料，实际 {ref_counts}"


async def test_no_answer_retry_can_be_disabled(embedder: FakeEmbedding, tmp_path) -> None:
    """拒答重试：no_answer_retry=1 时不再二次调用 LLM。"""
    rag = _make_rag(embedder, tmp_path, [("a.md", DOC_ZH)])
    no_payload = {"answer": "", "has_answer": False, "citations": [], "confidence": 0.05}
    chat = FakeChat(json.dumps(no_payload))
    gen = RagGenerator(rag, chat, top_k=1, min_score=0.0, no_answer_retry=1)

    ans = await gen.answer(QUERY_HIT)

    assert chat.call_count == 1
    assert ans.rejected_reason == "llm_no_answer"


async def test_reject_on_parse_error(rag: JwipcKnowledgeRAG) -> None:
    gen = RagGenerator(rag, FakeChat("这不是JSON"), min_score=0.0)

    ans = await gen.answer(QUERY_HIT)

    assert ans.has_answer is False
    assert ans.rejected_reason == "parse_error"
    assert ans.raw_llm_text == "这不是JSON"


async def test_reject_on_schema_violation(rag: JwipcKnowledgeRAG) -> None:
    payload = {"answer": "x", "has_answer": True, "citations": [], "confidence": 2.0}
    gen = RagGenerator(rag, FakeChat(json.dumps(payload)), min_score=0.0)

    ans = await gen.answer(QUERY_HIT)

    assert ans.rejected_reason == "parse_error"


async def test_reject_on_llm_error(rag: JwipcKnowledgeRAG) -> None:
    async def boom(messages: list[dict[str, str]]) -> str:
        raise RuntimeError("llm down")

    gen = RagGenerator(rag, boom, min_score=0.0)

    ans = await gen.answer(QUERY_HIT)

    assert ans.has_answer is False
    assert ans.rejected_reason == "llm_error"


# ---------------------------------------------------------------------------
# 引用校验
# ---------------------------------------------------------------------------


async def test_filters_invalid_citations(rag: JwipcKnowledgeRAG) -> None:
    chunk_id = rag.retrieve(QUERY_HIT, top_k=3)[0].chunk_id
    payload = {
        "answer": "Qdrant 是一个向量数据库。",
        "has_answer": True,
        "citations": [
            {"source": "qdrant_intro.md", "chunk_id": chunk_id, "quote": DOC_ZH},
            {"source": "hallucinated.md", "chunk_id": "fake-999", "quote": "不存在的片段"},
        ],
        "confidence": 0.8,
    }
    gen = RagGenerator(rag, FakeChat(json.dumps(payload, ensure_ascii=False)), min_score=0.0)

    ans = await gen.answer(QUERY_HIT)

    assert ans.has_answer is True
    assert len(ans.citations) == 1
    assert ans.citations[0].chunk_id == chunk_id


async def test_reject_when_all_citations_invalid(rag: JwipcKnowledgeRAG) -> None:
    payload = {
        "answer": "编造的答案",
        "has_answer": True,
        "citations": [{"source": "x.md", "chunk_id": "fake-1", "quote": "不存在的片段"}],
        "confidence": 0.9,
    }
    gen = RagGenerator(rag, FakeChat(json.dumps(payload)), min_score=0.0)

    ans = await gen.answer(QUERY_HIT)

    assert ans.has_answer is False
    assert ans.rejected_reason == "invalid_citations"
    assert ans.answer == NO_ANSWER_TEXT


async def test_remapped_citation_to_chunk_containing_quote(embedder: FakeEmbedding, tmp_path) -> None:
    """引用校验：chunk_id 标错但 quote 确有出处时，改挂到真正包含原文的片段。"""
    docs = [
        ("a.md", "Qdrant 是向量数据库，支持语义检索与 Payload 过滤。"),
        ("b.md", "Qdrant 支持混合检索与 Rerank 精排，适合 RAG 应用。"),
    ]
    rag = _make_rag(embedder, tmp_path, docs)
    top = rag.retrieve(QUERY_HIT, top_k=3)
    assert len(top) >= 2, "测试前提：至少召回两个片段"
    first, second = top[0], top[1]

    # chunk_id 指向 first，quote 却是 second 的原文 → 应改挂到 second
    payload = {
        "answer": "答案",
        "has_answer": True,
        "citations": [{"source": first.source, "chunk_id": first.chunk_id, "quote": second.text}],
        "confidence": 0.9,
    }
    gen = RagGenerator(rag, FakeChat(json.dumps(payload, ensure_ascii=False)), min_score=0.0)

    ans = await gen.answer(QUERY_HIT)

    assert ans.has_answer is True
    assert len(ans.citations) == 1
    assert ans.citations[0].chunk_id == second.chunk_id
    assert ans.citations[0].source == second.source


async def test_drops_citation_with_quote_from_other_chunk(rag: JwipcKnowledgeRAG) -> None:
    """引用校验：chunk_id 合法但 quote 不属于该片段原文时，该条引用被丢弃。"""
    chunk_id = rag.retrieve(QUERY_HIT, top_k=3)[0].chunk_id
    payload = {
        "answer": "Qdrant 是一个向量数据库。",
        "has_answer": True,
        "citations": [
            # quote 是编造的，不属于 DOC_ZH 原文 → 应被丢弃
            {"source": "qdrant_intro.md", "chunk_id": chunk_id, "quote": "这句话不在资料里"},
        ],
        "confidence": 0.9,
    }
    gen = RagGenerator(rag, FakeChat(json.dumps(payload, ensure_ascii=False)), min_score=0.0)

    ans = await gen.answer(QUERY_HIT)

    assert ans.has_answer is False
    assert ans.rejected_reason == "invalid_citations"


async def test_quote_matching_ignores_whitespace_and_case(rag: JwipcKnowledgeRAG) -> None:
    """引用校验：quote 改写换行 / 大小写 / 空白后仍应命中原文（不误杀）。"""
    chunk_id = rag.retrieve(QUERY_HIT, top_k=3)[0].chunk_id
    rewritten = DOC_ZH.replace("Qdrant", "qdrant").replace("，", "，\n ")
    assert rewritten != DOC_ZH, "测试前提：quote 应与原文存在空白 / 大小写差异"
    payload = {
        "answer": "Qdrant 是一个向量数据库。",
        "has_answer": True,
        "citations": [{"source": "qdrant_intro.md", "chunk_id": chunk_id, "quote": rewritten}],
        "confidence": 0.9,
    }
    gen = RagGenerator(rag, FakeChat(json.dumps(payload, ensure_ascii=False)), min_score=0.0)

    ans = await gen.answer(QUERY_HIT)

    assert ans.has_answer is True
    assert len(ans.citations) == 1


async def test_keeps_valid_citation_and_drops_mismatched_one(rag: JwipcKnowledgeRAG) -> None:
    """引用校验：混合场景——合法引用保留、错位引用丢弃，答案正常返回。"""
    top = rag.retrieve(QUERY_HIT, top_k=3)
    chunk_id = top[0].chunk_id
    other_text = next((r.text for r in top[1:] if r.text != top[0].text), None)
    payload = {
        "answer": "Qdrant 是一个向量数据库。",
        "has_answer": True,
        "citations": [
            {"source": "qdrant_intro.md", "chunk_id": chunk_id, "quote": DOC_ZH},
        ],
        "confidence": 0.9,
    }
    if other_text:
        payload["citations"].append(
            {"source": "qdrant_intro.md", "chunk_id": chunk_id, "quote": other_text}
        )
    gen = RagGenerator(rag, FakeChat(json.dumps(payload, ensure_ascii=False)), min_score=0.0)

    ans = await gen.answer(QUERY_HIT)

    assert ans.has_answer is True
    assert len(ans.citations) == 1
    assert ans.citations[0].quote == DOC_ZH


# ---------------------------------------------------------------------------
# Prompt 组装与 Schema 严格性
# ---------------------------------------------------------------------------


def test_build_rag_messages() -> None:
    ctx = [RetrievalResult(chunk_id="c1", source="a.md", score=0.9, text="hello")]

    msgs = build_rag_messages("你好", ctx)

    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert "has_answer" in msgs[0]["content"]
    assert "citations" in msgs[0]["content"]
    user = msgs[1]["content"]
    assert "[1] source=a.md chunk_id=c1" in user
    assert "你好" in user


def test_schema_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Citation(source="a", chunk_id="c", quote="q", junk=1)
    with pytest.raises(ValidationError):
        RagAnswer(answer="x", has_answer=True, junk=True)


def test_generator_validates_params(rag: JwipcKnowledgeRAG) -> None:
    with pytest.raises(ValueError):
        RagGenerator(rag, FakeChat("{}"), top_k=0)
    with pytest.raises(ValueError):
        RagGenerator(rag, FakeChat("{}"), min_score=1.5)
    with pytest.raises(TypeError):
        RagGenerator(rag, "not callable")
