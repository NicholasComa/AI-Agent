"""``POST /v1/rag/answer`` 契约、流式帧序与依赖不可用测试。"""

from __future__ import annotations

from pathlib import Path

import httpx
from service_fakes import (
    DOC_ZH,
    QUERY_HIT,
    QUERY_MISS,
    Harness,
    ServiceChat,
    build_deps,
)

from agent_service import AgentServiceSettings, create_agent_service_app
from rag.generator import NO_ANSWER_TEXT


async def test_answer_returns_citations(harness: Harness) -> None:
    """库内问题 → has_answer=true，引用指向召回片段，且只调一次模型。"""
    response = await harness.post(
        "/v1/rag/answer",
        {"question": QUERY_HIT, "min_score": 0.0},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["has_answer"] is True
    assert body["answer"] == "Qdrant 是一个向量数据库。"
    assert body["rejected_reason"] is None
    assert len(body["citations"]) == 1
    assert body["citations"][0]["source"] == "qdrant_intro.md"
    assert body["citations"][0]["quote"] == DOC_ZH
    assert harness.chat.call_count == 1


async def test_answer_low_score_rejects_without_llm(harness: Harness) -> None:
    """库外问题 → 召回侧拒答，且不调用模型（省成本、从源头防幻觉）。"""
    response = await harness.post("/v1/rag/answer", {"question": QUERY_MISS})
    assert response.status_code == 200
    body = response.json()
    assert body["has_answer"] is False
    assert body["answer"] == NO_ANSWER_TEXT
    assert body["rejected_reason"] == "low_score"
    assert harness.chat.call_count == 0


async def test_answer_rejects_invalid_payload(harness: Harness) -> None:
    """空问题与越界 top_k 在入口被拒，走统一错误信封。"""
    empty = await harness.post("/v1/rag/answer", {"question": ""})
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "invalid_argument"

    oversized = await harness.post("/v1/rag/answer", {"question": QUERY_HIT, "top_k": 99})
    assert oversized.status_code == 422
    assert oversized.json()["error"]["code"] == "invalid_argument"


async def test_stream_emits_delta_then_citations_then_done(harness: Harness) -> None:
    """流式帧序固定为 delta* → citations → done。"""
    frames = await harness.frames(
        "/v1/rag/answer",
        {"question": QUERY_HIT, "min_score": 0.0, "stream": True},
    )
    kinds = [frame["type"] for frame in frames]
    assert kinds[0] == "delta"
    assert kinds[-1] == "done"
    assert "citations" in kinds
    assert kinds.index("citations") < kinds.index("done")
    assert all(kind == "delta" for kind in kinds[: kinds.index("citations")])

    text = "".join(frame["text"] for frame in frames if frame["type"] == "delta")
    assert text == "Qdrant 是一个向量数据库。"
    citations_frame = frames[kinds.index("citations")]
    assert citations_frame["items"][0]["chunk_id"]
    assert frames[-1]["has_answer"] is True


async def test_answer_reports_dependency_unavailable_when_rag_missing(tmp_path: Path) -> None:
    """知识库未就绪 → 503 且指出缺哪一项，而不是 500。"""
    chat = ServiceChat("irrelevant")
    deps = build_deps(tmp_path, chat=chat)
    deps.rag = None
    deps.set_dependency("qdrant", ready=False, required=True, detail="not connected")
    app = create_agent_service_app(deps=deps, version="0.1.0")

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/v1/rag/answer",
            json={"question": QUERY_HIT, "min_score": 0.0},
        )
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "dependency_unavailable"
    assert "not connected" in error["detail"]


async def test_answer_uses_generator_default_min_score(harness: Harness) -> None:
    """未传 min_score 时沿用生成器默认阈值，而不是硬编码在路由里。"""
    response = await harness.post("/v1/rag/answer", {"question": QUERY_HIT})
    assert response.status_code == 200
    # 默认阈值下同一问题仍能命中（夹具语料与问题共享 token）
    assert response.json()["has_answer"] is True


async def test_answer_sets_trace_header_on_success(harness: Harness) -> None:
    """成功响应必须带 ``X-Trace-Id``。

    声明式返回模型（``response_model``）的实例不能被当作响应头载体：给它设
    未声明字段会抛 ``ValueError``，异常穿出 ``tracer.trace()`` 还会把根 span
    记成错误。因此头只能写在注入的 ``Response`` 上，这条用例把它钉住。
    """
    response = await harness.post("/v1/rag/answer", {"question": QUERY_HIT})
    assert response.status_code == 200
    trace_id = response.headers.get("X-Trace-Id")
    assert trace_id, "成功响应缺少 X-Trace-Id"
    assert len(trace_id) == 32


async def test_answer_successful_trace_is_not_marked_error(harness: Harness) -> None:
    """成功请求的根 span 不能是 error。

    回归防护：曾因给模型实例写 ``headers`` 抛出 ``ValueError``，使每个正常请求
    都留下一条 status=error 的根 span，评测算成功率会全错。
    """
    await harness.post("/v1/rag/answer", {"question": QUERY_HIT})

    rows = _read_trace_rows(harness.deps.tracer)
    roots = [row for row in rows if row.get("name") == "rag.answer" and row.get("kind") == "trace"]
    assert roots, "应当留下一条 rag.answer 的根 span"
    assert all(row["status"] == "ok" for row in roots)
    assert all(row.get("error_type") is None for row in roots)


async def test_answer_error_carries_trace_id_when_dependency_missing(tmp_path: Path) -> None:
    """503 也要带 trace 标识，且同时出现在响应头与错误信封里。

    异常处理器运行在根 span 收尾**之后**，此时「当前 trace」上下文已被复位，
    所以必须读「最近一次 trace」才能拿到标识——否则错误响应会丢掉这个最有用
    的排障线索。
    """
    deps = build_deps(tmp_path, chat=ServiceChat("irrelevant"))
    deps.rag = None
    deps.set_dependency("qdrant", ready=False, required=True, detail="not connected")
    app = create_agent_service_app(deps=deps, version="0.1.0")

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post("/v1/rag/answer", json={"question": QUERY_HIT})

    assert response.status_code == 503
    trace_id = response.headers.get("X-Trace-Id")
    assert trace_id, "503 响应缺少 X-Trace-Id"
    assert f"trace={trace_id}" in response.json()["error"]["detail"]


async def test_answer_stream_sets_trace_header(harness: Harness) -> None:
    """流式响应同样带 ``X-Trace-Id``（SSE 是自建响应对象，直接写头）。"""
    async with harness.client.stream(
        "POST", "/v1/rag/answer", json={"question": QUERY_HIT, "stream": True}
    ) as response:
        assert response.status_code == 200
        trace_id = response.headers.get("X-Trace-Id")
    assert trace_id, "流式响应缺少 X-Trace-Id"
    assert len(trace_id) == 32


def _read_trace_rows(tracer: object) -> list[dict[str, object]]:
    """把该追踪器已落盘的记录读成字典列表。"""
    import json

    backend = getattr(tracer, "backend", None)
    directory = getattr(backend, "directory", None)
    assert directory is not None, "测试后端应暴露落盘目录 directory"
    rows: list[dict[str, object]] = []
    for path in sorted(Path(directory).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def test_settings_session_dir_default_is_relative() -> None:
    """会话目录默认值必须是相对路径，否则容器里会写到镜像层而非 Volume。"""
    assert not AgentServiceSettings().session_dir.is_absolute()
