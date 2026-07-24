"""针对 :mod:`src.llm_client` 的测试。

所有测试都使用 ``httpx.MockTransport``，因此测试套件永远不会触网。
注入的 :class:`httpx.AsyncClient` 让每个测试都能完全控制状态码、响应体，
乃至传输层级别的异常。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest

from llm_client import (
    LlmAuthError,
    LlmClient,
    LlmError,
    LlmRateLimitError,
    LlmResponseFormatError,
    LlmResult,
    LlmServerError,
    LlmTimeoutError,
)

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs,
) -> LlmClient:
    """构造一个 HTTP 层被完全 mock 掉的 ``LlmClient``。

    传入 ``api_key=...`` 可覆盖默认的 ``"test-key"``。
    """
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    # ``api_key`` 在这里被消费；其余参数原样透传。
    api_key = kwargs.pop("api_key", "test-key")
    return LlmClient(
        base_url="https://api.example.com/v1",
        model="ai-mini",
        api_key=api_key,
        timeout_seconds=1.0,
        client=http_client,
        **kwargs,
    )


def _ok_handler(body: dict | None = None) -> Callable[[httpx.Request], httpx.Response]:
    """默认的 200 OK 处理器，回显一个合法的对话补全结果。"""
    payload = body or {
        "id": "chatcmpl-1",
        "model": "ai-mini",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi"}},
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


async def test_chat_returns_text_model_elapsed() -> None:
    client = _make_client(_ok_handler())
    try:
        result = await client.chat([{"role": "user", "content": "hello"}])
    finally:
        await client.aclose()

    assert isinstance(result, LlmResult)
    assert result.text == "hi"
    assert result.model == "ai-mini"
    assert result.elapsed_ms >= 0
    assert result.usage == {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


async def test_chat_sends_authorization_header() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["ct"] = request.headers.get("content-type")
        return httpx.Response(
            200,
            json={
                "model": "ai-mini",
                "choices": [{"message": {"role": "assistant", "content": "x"}}],
            },
        )

    client = _make_client(handler)
    try:
        await client.chat([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()

    assert captured["auth"] == "Bearer test-key"
    assert captured["ct"] == "application/json"


async def test_chat_request_body_has_model_and_messages() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "ai-mini",
                "choices": [{"message": {"role": "assistant", "content": "x"}}],
            },
        )

    client = _make_client(handler)
    try:
        await client.chat([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()

    assert captured["body"]["model"] == "ai-mini"
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]


async def test_chat_per_call_model_override() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "big-mini",
                "choices": [{"message": {"role": "assistant", "content": "x"}}],
            },
        )

    client = _make_client(handler)
    try:
        result = await client.chat([{"role": "user", "content": "hi"}], model="big-mini")
    finally:
        await client.aclose()

    assert captured["body"]["model"] == "big-mini"
    assert result.model == "big-mini"


async def test_chat_extra_body_merges_into_request() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _ok_handler()(request)

    client = _make_client(handler)
    try:
        await client.chat(
            [{"role": "user", "content": "hi"}],
            extra_body={"temperature": 0.2, "top_p": 0.9},
        )
    finally:
        await client.aclose()

    assert captured["body"]["temperature"] == 0.2
    assert captured["body"]["top_p"] == 0.9


# ---------------------------------------------------------------------------
# 鉴权（4xx，不重试）
# ---------------------------------------------------------------------------


async def test_401_raises_llm_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    client = _make_client(handler, max_retries=2)
    with pytest.raises(LlmAuthError, match="401"):
        await client.chat([{"role": "user", "content": "hi"}])


async def test_403_raises_llm_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    client = _make_client(handler)
    with pytest.raises(LlmAuthError, match="403"):
        await client.chat([{"role": "user", "content": "hi"}])


async def test_401_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    client = _make_client(handler, max_retries=3)
    with pytest.raises(LlmAuthError):
        await client.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1  # 401 绝不能触发重试


# ---------------------------------------------------------------------------
# 限流（429，重试）
# ---------------------------------------------------------------------------


async def test_429_retries_then_raises() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "rate limit"})

    client = _make_client(handler, max_retries=2, retry_backoff=0.001)
    with pytest.raises(LlmRateLimitError, match="rate limited"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 3  # 首次 + 2 次重试


async def test_429_eventually_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(
            200,
            json={
                "model": "ai-mini",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            },
        )

    client = _make_client(handler, max_retries=3, retry_backoff=0.001)
    try:
        result = await client.chat([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert result.text == "hi"
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# 5xx（重试）
# ---------------------------------------------------------------------------


async def test_500_retries_then_raises() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="internal error")

    client = _make_client(handler, max_retries=1, retry_backoff=0.001)
    with pytest.raises(LlmServerError, match="500"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2  # 首次 + 1 次重试


async def test_503_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(
            200,
            json={
                "model": "ai-mini",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
        )

    client = _make_client(handler, max_retries=2, retry_backoff=0.001)
    try:
        result = await client.chat([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert result.text == "ok"
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 超时
# ---------------------------------------------------------------------------


async def test_connect_timeout_retries_then_raises() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    client = _make_client(handler, max_retries=2, retry_backoff=0.001)
    with pytest.raises(LlmTimeoutError, match="connection failed"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 3


async def test_read_timeout_retries_then_raises() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("read timeout")

    client = _make_client(handler, max_retries=1, retry_backoff=0.001)
    with pytest.raises(LlmTimeoutError, match="timed out"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 响应格式错误
# ---------------------------------------------------------------------------


async def test_non_json_response_raises_format_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    client = _make_client(handler)
    with pytest.raises(LlmResponseFormatError, match="not valid JSON"):
        await client.chat([{"role": "user", "content": "hi"}])


async def test_missing_choices_raises_format_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "ai-mini"})

    client = _make_client(handler)
    with pytest.raises(LlmResponseFormatError, match="choices"):
        await client.chat([{"role": "user", "content": "hi"}])


async def test_non_string_content_raises_format_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "ai-mini",
                "choices": [{"message": {"role": "assistant", "content": 123}}],
            },
        )

    client = _make_client(handler)
    with pytest.raises(LlmResponseFormatError, match="string"):
        await client.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# 其它 4xx
# ---------------------------------------------------------------------------


async def test_400_not_retried_and_raises_llm_error() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    client = _make_client(handler, max_retries=2)
    with pytest.raises(LlmError, match="400"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# API 密钥处理
# ---------------------------------------------------------------------------


async def test_api_key_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "env-key")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "model": "ai-mini",
                "choices": [{"message": {"role": "assistant", "content": "x"}}],
            },
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = LlmClient(
        base_url="https://api.example.com/v1",
        model="ai-mini",
        client=http_client,
    )
    try:
        await client.chat([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert captured["auth"] == "Bearer env-key"


async def test_missing_api_key_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(LlmAuthError, match="API key not found"):
        LlmClient(base_url="https://api.example.com/v1", model="ai-mini")


async def test_api_key_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """即便遇到 500，API 密钥的值也绝不能出现在任何日志行里。"""
    secret = "sk-THISMUSTNOTAPPEAR1234567890"

    def handler(request: httpx.Request) -> httpx.Response:
        # 服务端可能会原样回显我们的 body，所以我们故意把密钥泄漏在那里。
        return httpx.Response(500, text=f"server saw key={secret}")

    client = _make_client(handler, api_key=secret, max_retries=0)
    with caplog.at_level(logging.WARNING), pytest.raises(LlmServerError):
        await client.chat([{"role": "user", "content": "hi"}])
    assert secret not in caplog.text


async def test_api_key_not_in_exception_message() -> None:
    """异常文本里不能包含密钥，即便服务端把它回显出来也不行。"""
    secret = "sk-THISMUSTNOTAPPEAR1234567890"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": f"key {secret} is bad"})

    client = _make_client(handler, api_key=secret, max_retries=0)
    with pytest.raises(LlmAuthError) as exc_info:
        await client.chat([{"role": "user", "content": "hi"}])
    assert secret not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 上下文管理器
# ---------------------------------------------------------------------------


async def test_async_context_manager() -> None:
    client = _make_client(_ok_handler())
    async with client as c:
        result = await c.chat([{"role": "user", "content": "hi"}])
    assert result.text == "hi"


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------


async def test_empty_messages_raises_value_error() -> None:
    client = _make_client(_ok_handler())
    with pytest.raises(ValueError, match="non-empty"):
        await client.chat([])


def test_constructor_validates_required_args() -> None:
    with pytest.raises(ValueError, match="base_url"):
        LlmClient(base_url="", model="m", api_key="k")
    with pytest.raises(ValueError, match="model"):
        LlmClient(base_url="https://x", model="", api_key="k")
    with pytest.raises(ValueError, match="max_retries"):
        LlmClient(base_url="https://x", model="m", api_key="k", max_retries=-1)
