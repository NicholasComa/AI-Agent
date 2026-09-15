"""闸门、超时、取消与幂等测试。

这些用例针对「服务工程语义」，不掺业务断言：并发超限应当被拒、总时限到期应当
返回明确错误、客户端断开应当停止推送、重复请求应当回放缓存。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from service_fakes import DOC_CHUNK_ID, QUERY_HIT, Harness, ServiceChat, build_deps

from agent_service import AgentServiceDeps, AgentServiceSettings, create_agent_service_app
from agent_service.guards import DisconnectProbe
from agent_service.routes.rag import build_generator, rag_frames
from agent_service.schemas import RagQueryRequest

_TIGHT_DEFAULTS: dict[str, Any] = {
    "max_concurrency": 1,
    "queue_timeout_seconds": 0.05,
    "request_timeout_seconds": 0.05,
}


def make_client(
    tmp_path: Path,
    **overrides: Any,
) -> tuple[httpx.AsyncClient, AgentServiceDeps]:
    """建一个闸门与超时参数收紧的客户端，用于快速触发 429 / 504。"""
    params = {**_TIGHT_DEFAULTS, **overrides}
    settings = AgentServiceSettings(session_dir=tmp_path / "sessions", **params)
    deps = build_deps(tmp_path, chat=ServiceChat(DOC_CHUNK_ID), settings=settings)
    app = create_agent_service_app(deps=deps, version="0.1.0")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"), deps


async def collect_frames(
    deps: AgentServiceDeps,
    probe: DisconnectProbe,
) -> list[dict[str, Any]]:
    """直接驱动 SSE 帧生成器，并把传输信封还原成业务帧。

    不经 HTTP 是为了能确定性地控制「客户端是否已断开」这个探针。
    """
    request_body = RagQueryRequest(question=QUERY_HIT, min_score=0.0, stream=True)
    raw = [
        item
        async for item in rag_frames(
            deps,
            generator=build_generator(request_body, deps),
            question=QUERY_HIT,
            probe=probe,
            request_id="rid",
        )
    ]
    return [json.loads(item["data"]) for item in raw]


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


async def test_idempotent_replay_returns_cached_body(harness: Harness) -> None:
    """带同一幂等键重发 → 回放缓存并附回放标记，不重复调用模型。"""
    headers = {"Idempotency-Key": "k-1"}
    payload = {"question": QUERY_HIT, "min_score": 0.0}

    first = await harness.post("/v1/rag/answer", payload, headers=headers)
    calls_after_first = harness.chat.call_count
    replay = await harness.post("/v1/rag/answer", payload, headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.headers.get("Idempotency-Replayed") == "true"
    assert replay.json() == first.json()
    assert harness.chat.call_count == calls_after_first


async def test_idempotent_different_body_is_not_replayed(harness: Harness) -> None:
    """同一幂等键但请求体不同 → 视为新请求，正常执行。"""
    headers = {"Idempotency-Key": "k-2"}
    await harness.post("/v1/rag/answer", {"question": QUERY_HIT, "min_score": 0.0}, headers=headers)
    response = await harness.post(
        "/v1/rag/answer",
        {"question": QUERY_HIT, "min_score": 0.0, "top_k": 5},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.headers.get("Idempotency-Replayed") is None


async def test_without_header_every_request_executes(harness: Harness) -> None:
    """没有幂等键时不做缓存，两次请求各执行一次。"""
    payload = {"question": QUERY_HIT, "min_score": 0.0}
    await harness.post("/v1/rag/answer", payload)
    before = harness.chat.call_count
    await harness.post("/v1/rag/answer", payload)
    assert harness.chat.call_count == before + 1


# ---------------------------------------------------------------------------
# 并发闸门与超时
# ---------------------------------------------------------------------------


async def test_gate_queue_timeout_returns_429(tmp_path: Path) -> None:
    """槽位被占满且排队超时 → 429 rate_limited，并给出可诊断的细节。"""
    client, deps = make_client(tmp_path)
    async with client:
        await deps.gate.acquire()  # 手工占满唯一槽位
        try:
            response = await client.post(
                "/v1/rag/answer",
                json={"question": QUERY_HIT, "min_score": 0.0},
            )
        finally:
            deps.gate.release()

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "rate_limited"
    assert "queue_timeout" in error["detail"]


async def test_gate_recovers_after_rejection(tmp_path: Path) -> None:
    """被拒一次后闸门必须回到可用状态，否则一次超限会把服务拖死。"""
    client, deps = make_client(tmp_path)
    async with client:
        await deps.gate.acquire()
        try:
            rejected = await client.post(
                "/v1/rag/answer",
                json={"question": QUERY_HIT, "min_score": 0.0},
            )
        finally:
            deps.gate.release()
        assert rejected.status_code == 429

        recovered = await client.post(
            "/v1/rag/answer",
            json={"question": QUERY_HIT, "min_score": 0.0},
        )
    assert recovered.status_code == 200


async def test_request_timeout_returns_504_and_releases_slot(tmp_path: Path) -> None:
    """处理超时 → 504 timeout，且槽位在超时后同样被释放。"""
    client, deps = make_client(tmp_path, request_timeout_seconds=0.05)
    deps.chat_fn = ServiceChat(DOC_CHUNK_ID, delay_seconds=0.3)

    async with client:
        response = await client.post(
            "/v1/rag/answer",
            json={"question": QUERY_HIT, "min_score": 0.0},
        )
        assert response.status_code == 504
        error = response.json()["error"]
        assert error["code"] == "timeout"
        assert "request_timeout" in error["detail"]
        assert deps.gate._value == deps.settings.max_concurrency


# ---------------------------------------------------------------------------
# 客户端断开与流式错误帧
# ---------------------------------------------------------------------------


def _stream_request() -> RagQueryRequest:
    return RagQueryRequest(question=QUERY_HIT, min_score=0.0, stream=True)


async def test_stream_stops_when_client_disconnected(tmp_path: Path) -> None:
    """断开探针一直为真 → 一帧都不再产出发送。

    直接驱动帧生成器，用探针替代真实连接，从而确定性地覆盖取消分支。
    """

    async def _always_disconnected() -> bool:
        return True

    deps = build_deps(tmp_path, chat=ServiceChat(DOC_CHUNK_ID))
    assert await collect_frames(deps, _always_disconnected) == []


async def test_stream_stops_mid_way_without_done_frame(tmp_path: Path) -> None:
    """探针在若干帧后翻转 → 帧被截断，且不会再补 done。"""
    calls = {"count": 0}

    async def _disconnect_after_first() -> bool:
        calls["count"] += 1
        return calls["count"] > 1

    chat = ServiceChat(DOC_CHUNK_ID, answer="一二三四五六七八九十" * 4)
    deps = build_deps(tmp_path, chat=chat)

    frames = await collect_frames(deps, _disconnect_after_first)
    assert [frame["type"] for frame in frames] == ["delta"]


async def test_stream_reports_gate_rejection_as_error_frame(tmp_path: Path) -> None:
    """流式模式下闸门拒绝不中断连接，而是先推 error 帧再推 done 帧。"""
    _client, deps = make_client(tmp_path)

    async def _never() -> bool:
        return False

    await deps.gate.acquire()
    try:
        frames = await collect_frames(deps, _never)
    finally:
        deps.gate.release()

    assert [frame["type"] for frame in frames] == ["error", "done"]
    assert frames[0]["code"] == "rate_limited"
    assert frames[1]["has_answer"] is False


async def test_generator_uses_request_top_k(tmp_path: Path) -> None:
    """请求参数透传到生成器，路由不自己再算一遍。"""
    deps = build_deps(tmp_path, chat=ServiceChat(DOC_CHUNK_ID))
    request_body = RagQueryRequest(question=QUERY_HIT, top_k=7, min_score=0.0)
    assert build_generator(request_body, deps).top_k == 7
