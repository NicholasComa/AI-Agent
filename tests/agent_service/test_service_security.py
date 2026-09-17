"""服务安全加固的行为测试：Bearer 认证、限流与请求体大小限制。

三类防护分处不同层次，用例也按层次组织：认证与限流走 HTTP 端到端；请求体
大小限制额外补一组 ASGI 层用例——分块传输没有 ``content-length``，只有直接
驱动中间件才能精确构造。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from service_fakes import QUERY_HIT, Harness, ServiceChat, build_deps

from agent_service import AgentServiceSettings, create_agent_service_app
from agent_service.security import BodySizeLimitMiddleware, TokenBucketRateLimiter

RAG_PATH = "/v1/rag/answer"
SECRET = "s3cret-api-key"
PAYLOAD: dict[str, Any] = {"question": QUERY_HIT, "min_score": 0.0}
OVERSIZED_PAYLOAD: dict[str, Any] = {"question": "x" * 300}
BEARER_HEADERS = {"Authorization": f"Bearer {SECRET}"}


class FakeClock:
    """可手动推进的单调时钟，让限流用例不依赖真实时间。"""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _settings(tmp_path: Path, **overrides: Any) -> AgentServiceSettings:
    return AgentServiceSettings(session_dir=tmp_path / "sessions", **overrides)


@asynccontextmanager
async def _running(
    tmp_path: Path,
    chunk_id: str,
    settings: AgentServiceSettings,
    *,
    limiter: TokenBucketRateLimiter | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """起一个带指定配置与限流器的应用，返回已就绪的客户端。"""
    deps = build_deps(tmp_path, chat=ServiceChat(chunk_id), settings=settings)
    if limiter is not None:
        deps.rate_limiter = limiter
    app = create_agent_service_app(deps=deps, version="0.1.0")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        yield client


# ---------------------------------------------------------------------------
# Bearer 认证
# ---------------------------------------------------------------------------


async def test_auth_disabled_by_default(harness: Harness) -> None:
    """未配置密钥时不做认证，默认配置开箱即用。"""
    response = await harness.post(RAG_PATH, PAYLOAD)
    assert response.status_code == 200


async def test_missing_bearer_token_is_rejected(tmp_path: Path, chunk_id: str) -> None:
    async with _running(tmp_path, chunk_id, _settings(tmp_path, api_key=SECRET)) as client:
        response = await client.post(RAG_PATH, json=PAYLOAD)

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "unauthorized"
    assert SECRET not in json.dumps(error, ensure_ascii=False)


async def test_wrong_bearer_token_is_rejected(tmp_path: Path, chunk_id: str) -> None:
    settings = _settings(tmp_path, api_key=SECRET)
    async with _running(tmp_path, chunk_id, settings) as client:
        response = await client.post(
            RAG_PATH, json=PAYLOAD, headers={"Authorization": "Bearer not-the-key"}
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_matching_bearer_token_is_accepted(tmp_path: Path, chunk_id: str) -> None:
    settings = _settings(tmp_path, api_key=SECRET)
    async with _running(tmp_path, chunk_id, settings) as client:
        response = await client.post(RAG_PATH, json=PAYLOAD, headers=BEARER_HEADERS)

    assert response.status_code == 200
    assert response.json()["has_answer"] is True


async def test_bearer_scheme_is_case_insensitive(tmp_path: Path, chunk_id: str) -> None:
    """``Bearer`` / ``bearer`` 都是合法写法，按 RFC 6750 方案名不区分大小写。"""
    settings = _settings(tmp_path, api_key=SECRET)
    async with _running(tmp_path, chunk_id, settings) as client:
        response = await client.post(
            RAG_PATH, json=PAYLOAD, headers={"Authorization": f"bearer {SECRET}"}
        )

    assert response.status_code == 200


async def test_non_bearer_scheme_is_rejected(tmp_path: Path, chunk_id: str) -> None:
    settings = _settings(tmp_path, api_key=SECRET)
    async with _running(tmp_path, chunk_id, settings) as client:
        response = await client.post(
            RAG_PATH, json=PAYLOAD, headers={"Authorization": f"Basic {SECRET}"}
        )

    assert response.status_code == 401


async def test_probes_bypass_auth(tmp_path: Path, chunk_id: str) -> None:
    """探针必须能在认证开启时照常作答，否则编排系统会误判进程状态。

    判据用白名单而非「非 401 即通过」：/ready 在必需依赖未就绪时设计上返回
    503（见 ``routes/health.py``），是合法应答；而 500 / 502 / 404 属于探针
    自身故障，必须判失败。
    """
    settings = _settings(tmp_path, api_key=SECRET)
    async with _running(tmp_path, chunk_id, settings) as client:
        health = await client.get("/health")
        ready = await client.get("/ready")

    assert health.status_code == 200
    assert ready.status_code in {200, 503}


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------


async def test_rate_limit_blocks_after_quota_with_retry_after(
    tmp_path: Path, chunk_id: str
) -> None:
    settings = _settings(tmp_path, rate_limit_per_minute=2)
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(capacity=2, clock=clock)

    async with _running(tmp_path, chunk_id, settings, limiter=limiter) as client:
        allowed = [await client.post(RAG_PATH, json=PAYLOAD) for _ in range(2)]
        blocked = await client.post(RAG_PATH, json=PAYLOAD)

    assert [response.status_code for response in allowed] == [200, 200]
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) >= 1


async def test_rate_limit_quota_is_per_client(tmp_path: Path, chunk_id: str) -> None:
    """不同 ``X-Client-Id`` 各占一个桶，互不挤占配额。"""
    settings = _settings(tmp_path, rate_limit_per_minute=1)
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(capacity=1, clock=clock)

    async with _running(tmp_path, chunk_id, settings, limiter=limiter) as client:
        first = await client.post(RAG_PATH, json=PAYLOAD, headers={"X-Client-Id": "alpha"})
        exhausted = await client.post(RAG_PATH, json=PAYLOAD, headers={"X-Client-Id": "alpha"})
        other = await client.post(RAG_PATH, json=PAYLOAD, headers={"X-Client-Id": "beta"})

    assert first.status_code == 200
    assert exhausted.status_code == 429
    assert other.status_code == 200


async def test_probes_are_not_rate_limited(tmp_path: Path, chunk_id: str) -> None:
    """探针不挂限流，容器健康检查不会被业务配额挤掉。"""
    settings = _settings(tmp_path, rate_limit_per_minute=1)
    limiter = TokenBucketRateLimiter(capacity=1, clock=FakeClock())

    async with _running(tmp_path, chunk_id, settings, limiter=limiter) as client:
        statuses = [(await client.get("/health")).status_code for _ in range(3)]

    assert statuses == [200, 200, 200]


def test_token_bucket_refills_over_time() -> None:
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(capacity=2, clock=clock)

    assert [limiter.check("c") for _ in range(2)] == [0.0, 0.0]
    assert limiter.check("c") > 0.0

    # 容量 2 / 窗口 60 秒 => 每 30 秒补一个令牌。
    clock.advance(31.0)
    assert limiter.check("c") == 0.0


def test_token_bucket_rejects_non_positive_capacity() -> None:
    with pytest.raises(ValueError, match="capacity"):
        TokenBucketRateLimiter(capacity=0)


def test_token_bucket_bounds_tracked_clients() -> None:
    """跟踪表超过上限时丢弃已回满的桶，避免无限增长。"""
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(capacity=1, clock=clock)
    for index in range(TokenBucketRateLimiter.MAX_TRACKED_CLIENTS + 1):
        limiter.check(f"client-{index}")

    # 每检查一个新客户端前都触发一次清理，因此表长不会超过上限加一。
    assert limiter.tracked_clients() <= TokenBucketRateLimiter.MAX_TRACKED_CLIENTS + 1


# ---------------------------------------------------------------------------
# 请求体大小限制
# ---------------------------------------------------------------------------


class _Downstream:
    """最小下游应用：读完请求体后回 200，并记录收到的字节块。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.receive_calls = 0
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        self.receive_calls += 1
        if self._chunks:
            body = self._chunks.pop(0)
            return {
                "type": "http.request",
                "body": body,
                "more_body": bool(self._chunks),
            }
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _scope(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {"type": "http", "method": "POST", "path": "/x", "headers": headers}


async def test_oversized_declared_body_is_rejected(tmp_path: Path, chunk_id: str) -> None:
    settings = _settings(tmp_path, max_body_bytes=128)
    async with _running(tmp_path, chunk_id, settings) as client:
        response = await client.post(RAG_PATH, json=OVERSIZED_PAYLOAD)

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "payload_too_large"
    assert "128" in (error["detail"] or "")


async def test_body_within_limit_is_accepted(tmp_path: Path, chunk_id: str) -> None:
    settings = _settings(tmp_path, max_body_bytes=128)
    async with _running(tmp_path, chunk_id, settings) as client:
        response = await client.post(RAG_PATH, json=PAYLOAD)

    assert response.status_code == 200


async def test_content_length_is_checked_before_reading_body() -> None:
    """声明超限时直接拒绝，请求体一个字节都不读。"""
    downstream = _Downstream([])
    middleware = BodySizeLimitMiddleware(downstream, default_max_bytes=16)
    scope = _scope([(b"content-length", b"9999")])

    await middleware(scope, downstream.receive, downstream.send)

    assert downstream.receive_calls == 0
    assert downstream.sent[0]["status"] == 413
    payload = json.loads(downstream.sent[1]["body"])
    assert payload["error"]["code"] == "payload_too_large"


async def test_chunked_body_over_limit_is_rejected() -> None:
    """未声明 ``content-length`` 时按读取过程中的累计字节数判定。"""
    downstream = _Downstream([b"x" * 40, b"x" * 40, b"x" * 40])
    middleware = BodySizeLimitMiddleware(downstream, default_max_bytes=64)
    scope = _scope([(b"transfer-encoding", b"chunked")])

    await middleware(scope, downstream.receive, downstream.send)

    assert downstream.sent[0]["status"] == 413
    payload = json.loads(downstream.sent[1]["body"])
    assert payload["error"]["code"] == "payload_too_large"


async def test_chunked_body_within_limit_is_accepted() -> None:
    downstream = _Downstream([b"x" * 20, b"y" * 20])
    middleware = BodySizeLimitMiddleware(downstream, default_max_bytes=64)
    scope = _scope([(b"transfer-encoding", b"chunked")])

    await middleware(scope, downstream.receive, downstream.send)

    assert downstream.sent[0]["status"] == 200
