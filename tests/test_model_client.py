"""针对 :class:`model_client.ModelClient` 协议的契约测试。

这些测试既验证 :class:`LlmClient` 自动满足协议（结构子类型），
也通过注入 :class:`httpx.MockTransport` 验证协议要求的所有行为契约:
成功 / 401 不重试 / 429 重试成功 / 5xx 重试耗尽 / 超时 / 响应格式错误。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from llm_client import (
    LlmAuthError,
    LlmClient,
    LlmResponseFormatError,
    LlmServerError,
    LlmTimeoutError,
)
from model_client import ModelClient

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response | Exception],
    **kwargs,
) -> LlmClient:
    """构造一个 HTTP 层被完全 mock 掉的 :class:`LlmClient`。

    ``handler`` 可以返回 :class:`httpx.Response`,也可以直接抛异常
    （用来模拟 ``httpx.TimeoutException`` 等传输层失败）。
    """
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    api_key = kwargs.pop("api_key", "test-key")
    return LlmClient(
        base_url="https://api.example.com/v1",
        model="ai-mini",
        api_key=api_key,
        timeout_seconds=1.0,
        client=http_client,
        **kwargs,
    )


def _ok_payload(text: str = "hi", model: str = "ai-mini") -> dict:
    return {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


# ---------------------------------------------------------------------------
# 协议满足性
# ---------------------------------------------------------------------------


def test_llm_client_satisfies_protocol() -> None:
    """LlmClient 是 :class:`ModelClient` 的结构子类型（@runtime_checkable）。"""
    client = _make_client(lambda r: httpx.Response(200, json=_ok_payload()))
    try:
        # 协议是结构子类型,不需要继承。isinstance 验证 @runtime_checkable 工作。
        assert isinstance(client, ModelClient)
    finally:
        # 不通过 aclose() 走 _owns_client 分支,手动关闭 mock client
        pass


# ---------------------------------------------------------------------------
# 协议契约
# ---------------------------------------------------------------------------


async def test_protocol_chat_success() -> None:
    """200 OK → 返回 LlmResult。"""
    client = _make_client(lambda r: httpx.Response(200, json=_ok_payload("hi")))
    try:
        result = await client.chat([{"role": "user", "content": "q"}])
    finally:
        await client.aclose()

    assert result.text == "hi"
    assert result.model == "ai-mini"


async def test_protocol_401_no_retry() -> None:
    """401 → LlmAuthError,handler 仅被调用 1 次（不重试）。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    client = _make_client(handler, max_retries=3)
    try:
        with pytest.raises(LlmAuthError):
            await client.chat([{"role": "user", "content": "q"}])
    finally:
        await client.aclose()

    assert call_count["n"] == 1, f"401 should not retry, got {call_count['n']} calls"


async def test_protocol_429_retries_then_success() -> None:
    """首次 429 → 重试 → 200 → 成功,handler 调用 2 次。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=_ok_payload("retried ok"))

    client = _make_client(handler, max_retries=2, retry_backoff=0.001)
    try:
        result = await client.chat([{"role": "user", "content": "q"}])
    finally:
        await client.aclose()

    assert result.text == "retried ok"
    assert call_count["n"] == 2


async def test_protocol_5xx_retries_exhausted() -> None:
    """持续 503 → 重试耗尽 → LlmServerError,handler 调用 1+max_retries 次。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(503, text="service unavailable")

    client = _make_client(handler, max_retries=2, retry_backoff=0.001)
    try:
        with pytest.raises(LlmServerError):
            await client.chat([{"role": "user", "content": "q"}])
    finally:
        await client.aclose()

    assert call_count["n"] == 3, f"503 should retry max_retries+1=3 times, got {call_count['n']}"


async def test_protocol_timeout_raises() -> None:
    """连接超时 → 重试耗尽 → LlmTimeoutError。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        raise httpx.ConnectTimeout("connect timed out")

    client = _make_client(handler, max_retries=1, retry_backoff=0.001)
    try:
        with pytest.raises(LlmTimeoutError):
            await client.chat([{"role": "user", "content": "q"}])
    finally:
        await client.aclose()

    assert call_count["n"] == 2


async def test_protocol_bad_json_raises() -> None:
    """200 + 非 JSON body → LlmResponseFormatError,不重试。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, content=b"<html>not json</html>")

    client = _make_client(handler, max_retries=2)
    try:
        with pytest.raises(LlmResponseFormatError):
            await client.chat([{"role": "user", "content": "q"}])
    finally:
        await client.aclose()

    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


async def test_factory_builds_model_client() -> None:
    """:func:`model_factory.build_model_client` 返回 :class:`ModelClient`。"""
    from config import AppConfig
    from model_factory import build_model_client

    settings = AppConfig.model_validate(
        {
            "api_base_url": "https://api.example.com/v1",
            "model_name": "ai-mini",
            "api_key": "sk-factory-test",
        }
    )
    client = build_model_client(settings)
    try:
        assert isinstance(client, ModelClient)
        assert isinstance(client, LlmClient)
        assert client.model == "ai-mini"
        assert client.base_url == "https://api.example.com/v1"
    finally:
        await client.aclose()
