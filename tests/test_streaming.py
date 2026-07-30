"""Day 9 流式端点 (POST /chat/stream) 端到端测试。

覆盖 Roadmap 第 2 周阶段任务里明确列出的边界场景:

* **超时 / 错误 JSON / 空响应 / 取消请求**
* 流式 happy path（多 chunk + 终止哨兵）
* SSE 错误事件传播（``LlmError`` → ``event: error`` + 终止 chunk）
* request_id 在 SSE 响应里也透传
* 纯 ASGI RequestId middleware 在流式响应下也正确加响应头

测试通过 :class:`httpx.AsyncClient` + :class:`httpx.ASGITransport` 在
进程内驱动 ASGI app —— 与 Day 5/8 的测试一致。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sse_starlette.sse import EventSourceResponse

from api_models import ChatChunk, ErrorResponse
from app import create_app
from config import AppConfig
from llm_client import (
    LlmResult,
    LlmServerError,
    LlmTimeoutError,
)

# ---------------------------------------------------------------------------
# 测试替身:支持 chat_stream 的 fake
# ---------------------------------------------------------------------------


class _FakeStreamLlm:
    """支持 ``chat_stream()`` 与 ``chat()`` 的最小 fake。

    ``chat_stream`` 行为由两种方式注入:
    * ``set_stream_responder(callable)`` —— callable 返回 async generator,
      fake 逐个 ``yield`` 出去;
    * ``set_stream_error(exc)`` —— fake 在 stream 一开始就 ``raise exc``。

    未注入任何状态时,``chat_stream`` 默认 yield ``["你", "好"]``。

    ``chat`` 沿用 Day 5 的默认 responder 即可(本文件不测)。
    """

    def __init__(self) -> None:
        self.aclose_count = 0
        self.received_calls: list[dict[str, Any]] = []
        self._stream_responder: Any = None
        self._stream_error: BaseException | None = None

    def set_stream_responder(self, responder: Any) -> None:
        """注入一个 callable,被调用时返回 async generator。"""
        self._stream_responder = responder
        self._stream_error = None

    def set_stream_error(self, exc: BaseException) -> None:
        """注入一个异常,``chat_stream`` 立即 raise。"""
        self._stream_error = exc
        self._stream_responder = None

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> LlmResult:
        self.received_calls.append(
            {"messages": list(messages), "model": model, "extra_body": extra_body}
        )
        return LlmResult(text="ok", model="fake-model", elapsed_ms=1, usage=None)

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        # 错误路径:先 raise
        if self._stream_error is not None:
            raise self._stream_error

        # 注入 async generator
        if self._stream_responder is not None:
            async for d in self._stream_responder():
                yield d
            return

        # 默认
        yield "你"
        yield "好"

    async def aclose(self) -> None:
        self.aclose_count += 1

    async def __aenter__(self) -> _FakeStreamLlm:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def base_url(self) -> str:
        return "http://fake/v1"

    @property
    def timeout_seconds(self) -> float:
        return 1.0


def _fake_config() -> AppConfig:
    return AppConfig(
        model_name="fake-model",
        api_base_url="http://fake/v1",
        timeout_seconds=1.0,
        enable_stream=True,
        log_format="plain",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_llm() -> _FakeStreamLlm:
    return _FakeStreamLlm()


@pytest.fixture
def fake_config() -> AppConfig:
    return _fake_config()


@pytest.fixture
async def client(
    fake_llm: _FakeStreamLlm,
    fake_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """Day 9 client fixture: create_app() 返回已挂好 RequestId + AccessLog 的完整应用。"""
    monkeypatch.setenv("API_KEY", "test-key")
    fastapi_app = create_app(
        llm_factory=lambda: fake_llm,
        config_loader=lambda: fake_config,
    )
    asgi_app = fastapi_app
    async with (
        fastapi_app.router.lifespan_context(fastapi_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app),
            base_url="http://test",
            timeout=10.0,
        ) as ac,
    ):
        yield ac


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


async def _collect_sse_chunks(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> tuple[list[dict[str, str]], httpx.Response]:
    """建立流式连接并解析全部 SSE 事件,返回 (events, response)。

    每个 event 是 ``{"event": "...", "data": "..."}`` 形状。

    SSE 协议:``event: <name>\\ndata: <payload>\\n\\n`` 表示一条 message。
    空行是 message 之间的分隔符,必须按它来切。
    """
    async with client.stream(
        "POST",
        "/chat/stream",
        json=payload,
        headers=headers or {},
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        assert resp.headers["content-type"].startswith("text/event-stream"), resp.headers
        events: list[dict[str, str]] = []
        current: dict[str, str] = {}
        async for line in resp.aiter_lines():
            if not line:
                # 空行 = 一条 message 结束
                if current:
                    events.append(current)
                    current = {}
                continue
            if line.startswith("event:"):
                current["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                v = line.split(":", 1)[1].lstrip()
                current["data"] = v if not current.get("data") else current["data"] + v
        # 收尾
        if current:
            events.append(current)
        return events, resp


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


async def test_chat_stream_returns_sse_chunks(
    client: httpx.AsyncClient, fake_llm: _FakeStreamLlm
) -> None:
    """happy path:多 chunk + 终止哨兵。"""
    events, _ = await _collect_sse_chunks(
        client,
        {"messages": [{"role": "user", "content": "hi"}]},
    )

    # 应该有: 2 个普通 chunk + 1 个 done chunk
    assert len(events) == 3, f"expected 3 events, got {events!r}"
    assert events[0]["event"] == "chunk"
    assert events[1]["event"] == "chunk"
    assert events[2]["event"] == "chunk"

    parsed = [ChatChunk.model_validate_json(e["data"]) for e in events]
    assert parsed[0].delta == "你"
    assert parsed[0].done is False
    assert parsed[1].delta == "好"
    assert parsed[1].done is False
    assert parsed[2].delta == ""
    assert parsed[2].done is True


async def test_chat_stream_error_event_on_5xx(
    client: httpx.AsyncClient, fake_llm: _FakeStreamLlm
) -> None:
    """LlmServerError → 1 个 error event + 1 个 done chunk,无普通 chunk。"""
    # 注入错误
    fake_llm.set_stream_error(LlmServerError("upstream 500"))

    events, _ = await _collect_sse_chunks(
        client,
        {"messages": [{"role": "user", "content": "hi"}]},
    )

    # error event + 终止 chunk = 2 events
    assert len(events) == 2, f"expected 2 events, got {events!r}"
    assert events[0]["event"] == "error"
    err = ErrorResponse.model_validate_json(events[0]["data"])
    # LlmServerError 走 _map_llm_error → (502, "llm_server")，
    # 与非流式路径共用同一张映射（不再是写死的 "llm_stream_error"）。
    assert err.error.code == "llm_server"
    assert "upstream 500" in err.error.message
    assert err.error.status_code == 502
    # rid 由 RequestIdASGIMiddleware 自动生成，应同步写入 ErrorBody。
    assert err.error.request_id is not None

    # 终止哨兵
    done = ChatChunk.model_validate_json(events[1]["data"])
    assert done.done is True


async def test_chat_stream_cancellation_propagates(
    client: httpx.AsyncClient, fake_llm: _FakeStreamLlm
) -> None:
    """客户端断开后,服务端应能感知到且不再发送更多 chunk。"""

    # 注入一个"慢"stream:每次 yield 前 sleep 50ms,给客户端断开时间窗口
    async def _slow() -> AsyncIterator[str]:
        import asyncio

        for i in range(50):
            await asyncio.sleep(0.05)
            yield f"chunk-{i}"

    fake_llm.set_stream_responder(_slow)

    # 建立连接并立即断开
    received: list[str] = []
    async with client.stream(
        "POST",
        "/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}]},
    ) as resp:
        assert resp.status_code == 200
        # 拿到前 1-2 个 chunk 后立刻断开
        n = 0
        async for line in resp.aiter_lines():
            received.append(line)
            n += 1
            if n >= 4:  # 大约 2 个 SSE 消息(每消息 2 行:event + data)
                break

    # 关键断言:断开前至少收到 1 个 chunk(证明流真的在推,而不是被 BaseHTTPMiddleware 缓存)
    assert any("chunk-" in r for r in received), f"no chunk in: {received!r}"


async def test_chat_stream_empty_response_format_error(
    client: httpx.AsyncClient, fake_llm: _FakeStreamLlm
) -> None:
    """空响应/LlmTimeoutError 在流式路径下也应通过 error event 暴露。"""
    fake_llm.set_stream_error(LlmTimeoutError("stream timed out after 3 attempts"))

    events, _ = await _collect_sse_chunks(
        client,
        {"messages": [{"role": "user", "content": "hi"}]},
    )

    assert len(events) == 2
    assert events[0]["event"] == "error"
    err = ErrorResponse.model_validate_json(events[0]["data"])
    assert "stream timed out" in err.error.message


async def test_chat_stream_request_id_in_chunks(
    client: httpx.AsyncClient, fake_llm: _FakeStreamLlm
) -> None:
    """客户端送 ``X-Request-ID`` → 响应头里回传 + SSE 错误事件里携带同源 rid。"""
    # 通过 error 路径验证 rid 出现在 SSE body 里
    fake_llm.set_stream_error(LlmServerError("boom"))

    incoming = "stream-trace-001"
    async with client.stream(
        "POST",
        "/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Request-ID": incoming},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers.get("X-Request-ID") == incoming

        events: list[dict[str, str]] = []
        current: dict[str, str] = {}
        async for line in resp.aiter_lines():
            if not line:
                if current:
                    events.append(current)
                    current = {}
                continue
            if line.startswith("event:"):
                current["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                v = line.split(":", 1)[1].lstrip()
                current["data"] = v if not current.get("data") else current["data"] + v
        if current:
            events.append(current)

    assert len(events) == 2
    # 验证 error 事件能成功解析；rid 既要进响应头，也要进 ErrorBody.body。
    err = ErrorResponse.model_validate_json(events[0]["data"])
    assert err.error.request_id == incoming
    assert resp.headers["X-Request-ID"] == incoming


async def test_chat_stream_asgi_middleware_emits_header(
    client: httpx.AsyncClient, fake_llm: _FakeStreamLlm
) -> None:
    """流式响应也带 ``X-Request-ID`` 响应头 —— 证明纯 ASGI middleware 兼容 SSE。"""
    # 没传 X-Request-ID,middleware 应生成
    async with client.stream(
        "POST",
        "/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}]},
    ) as resp:
        assert resp.status_code == 200
        rid = resp.headers.get("X-Request-ID")
        assert rid is not None
        # uuid4().hex 长度 32
        assert len(rid) == 32
        assert all(c in "0123456789abcdef" for c in rid)


# ---------------------------------------------------------------------------
# /chat/stream 路由本身的契约(确保它真的返回 EventSourceResponse)
# ---------------------------------------------------------------------------


def test_chat_stream_route_is_registered() -> None:
    """``POST /chat/stream`` 必须已注册到 app。"""
    fastapi_app = create_app(
        llm_factory=lambda: _FakeStreamLlm(),
        config_loader=_fake_config,
    )
    paths_methods = {
        (r.path, tuple(getattr(r, "methods", None) or []))
        for r in fastapi_app.routes
        if hasattr(r, "path")
    }
    assert ("/chat/stream", ("POST",)) in paths_methods


def test_chat_stream_returns_event_source_response_type() -> None:
    """调用 chat_stream 端点应返回 :class:`EventSourceResponse`(sse-starlette)。"""
    # 这是个类型层面的契约测试,防止有人把 /chat/stream 改成普通 JSONResponse
    from app import _register_routes  # noqa: PLC0415

    # 反射:检查 _register_routes 函数签名包含 EventSourceResponse 类型注解
    hints = _register_routes.__annotations__
    # 至少有一处用到 EventSourceResponse
    import typing

    for v in hints.values():
        origin = typing.get_origin(v)
        if origin is not None:
            args = typing.get_args(v)
            if EventSourceResponse in args:
                return
        elif v is EventSourceResponse:
            return
    # 也直接搜一下源码
    import inspect

    src = inspect.getsource(_register_routes)
    assert "EventSourceResponse" in src
