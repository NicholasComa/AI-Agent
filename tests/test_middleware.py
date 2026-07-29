"""Day 8 中间件层测试。

聚焦三件事:

1. ``X-Request-ID`` 的生成 / 回传 / 透传到响应体。
2. 访问日志**绝不**泄露 ``Authorization`` 头或请求体里的敏感内容。
3. ``request_id`` 通过 :data:`logging_config.request_id_var` 在路由
   handler 内也能读到(证明 middleware → contextvar → handler 链路通)。

测试用 :class:`httpx.AsyncClient` + :class:`httpx.ASGITransport` 在
进程内驱动 ASGI app,不走真实 socket。每个用例都拿自己的 app 实例,
``caplog`` 在 pytest fixture 作用下捕获所有 root-logger 的 record。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app import create_app
from config import AppConfig
from llm_client import LlmResult

# ---------------------------------------------------------------------------
# 测试替身:与 test_api.py 保持一致 —— 避免真模型被调起
# ---------------------------------------------------------------------------


class _FakeLlm:
    """最小可用的 LLM 替身。"""

    def __init__(self) -> None:
        self.received_calls: list[dict[str, Any]] = []

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
        return LlmResult(
            text="ok",
            model="fake-model",
            elapsed_ms=1,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> _FakeLlm:
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
        enable_stream=False,
        log_format="plain",  # 测试里把 JSON 关掉,caplog 输出更干净
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_llm() -> _FakeLlm:
    return _FakeLlm()


@pytest.fixture
def fake_config() -> AppConfig:
    return _fake_config()


@pytest.fixture
async def client(
    fake_llm: _FakeLlm,
    fake_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """构建一个注入了 fake 的 app + 驱动 lifespan 的 httpx 客户端。"""
    monkeypatch.setenv("API_KEY", "test-key")
    app = create_app(
        llm_factory=lambda: fake_llm,
        config_loader=lambda: fake_config,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as ac,
    ):
        yield ac


# ---------------------------------------------------------------------------
# request_id 生成 / 回传
# ---------------------------------------------------------------------------


async def test_request_id_generated_when_absent(client: httpx.AsyncClient) -> None:
    """无 ``X-Request-ID`` → middleware 生成 32 字符 hex 并回传到响应头。"""
    resp = await client.get("/health")
    assert resp.status_code == 200

    rid = resp.headers.get("X-Request-ID")
    assert rid is not None, "X-Request-ID missing from response headers"
    # uuid4().hex 长度为 32
    assert len(rid) == 32, f"expected 32-char hex, got {rid!r}"
    # 全是 hex 字符
    assert all(c in "0123456789abcdef" for c in rid), f"non-hex chars in {rid!r}"


async def test_request_id_echoed_when_provided(client: httpx.AsyncClient) -> None:
    """客户端送 ``X-Request-ID: my-trace-123`` → 响应头原样回传。"""
    incoming = "my-trace-12345"
    resp = await client.get("/health", headers={"X-Request-ID": incoming})
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == incoming


async def test_request_id_in_response_body(client: httpx.AsyncClient) -> None:
    """响应体里的 ``request_id`` 字段 == 响应头 ``X-Request-ID``。"""
    incoming = "trace-in-body-abc"
    resp = await client.get("/health", headers={"X-Request-ID": incoming})
    assert resp.status_code == 200

    body = resp.json()
    assert body["request_id"] == incoming
    assert body["request_id"] == resp.headers["X-Request-ID"]


async def test_chat_response_carries_request_id(
    client: httpx.AsyncClient, fake_llm: _FakeLlm
) -> None:
    """``/chat`` 响应体里也携带 ``request_id``(与响应头同源)。"""
    incoming = "trace-chat-001"
    resp = await client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Request-ID": incoming},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request_id"] == incoming
    assert resp.headers["X-Request-ID"] == incoming


# ---------------------------------------------------------------------------
# 访问日志脱敏
# ---------------------------------------------------------------------------


async def test_access_log_redacts_authorization(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """访问日志绝不包含 ``Authorization`` 头的值。"""
    secret = "sk-SECRET-CAN-NEVER-APPEAR-1234567890"
    with caplog.at_level(logging.INFO, logger="middleware"):
        resp = await client.get(
            "/health",
            headers={"Authorization": f"Bearer {secret}"},
        )
    assert resp.status_code == 200
    # 关键断言:密钥绝不能出现在 caplog 的任何一行里
    assert secret not in caplog.text
    assert "Bearer " + secret not in caplog.text
    # 但 access 行应该被记下(便于测试通过 path 字段确认日志机制在跑)
    access_records = [r for r in caplog.records if r.message == "access"]
    assert len(access_records) == 1
    assert access_records[0].path == "/health"
    assert access_records[0].method == "GET"
    assert access_records[0].status_code == 200


async def test_access_log_carries_request_id(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """访问日志的 ``request_id`` 必须等于响应头 ``X-Request-ID``(而非默认的 '-')。

    这是 AccessLogMiddleware 修复点的回归测试:本中间件是最外层,记日志时
    RequestIdMiddleware 已将 request_id_var reset 回 None,因此必须从
    ``request.state.request_id`` 读取并通过 extra 透传,不能再依赖 ContextVar。
    """
    incoming = "access-log-trace-001"
    with caplog.at_level(logging.INFO, logger="middleware"):
        resp = await client.get("/health", headers={"X-Request-ID": incoming})
    assert resp.status_code == 200

    access_records = [r for r in caplog.records if r.message == "access"]
    assert len(access_records) == 1
    # 关键断言:request_id 必须透传成功,绝不能是 reset 后的 '-'
    assert access_records[0].request_id == incoming
    assert access_records[0].request_id == resp.headers["X-Request-ID"]


async def test_access_log_request_id_defaults_to_generated(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """未提供 X-Request-ID 时,访问日志的 request_id 应为生成的 32 字符 hex。"""
    with caplog.at_level(logging.INFO, logger="middleware"):
        resp = await client.get("/health")
    assert resp.status_code == 200

    access_records = [r for r in caplog.records if r.message == "access"]
    assert len(access_records) == 1
    rid = access_records[0].request_id
    assert rid is not None and rid != "-"
    assert len(rid) == 32
    assert all(c in "0123456789abcdef" for c in rid)
    assert rid == resp.headers["X-Request-ID"]


async def test_access_log_redacts_messages_content(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """访问日志绝不包含 POST 请求体里的内容(密钥、提示词等)。"""
    secret = "PROMPT-SECRET-LEAK-MARKER-XYZ-9999"
    with caplog.at_level(logging.INFO, logger="middleware"):
        resp = await client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": secret}]},
        )
    assert resp.status_code == 200, resp.text
    assert secret not in caplog.text
    # access 行仍应被记下
    access_records = [r for r in caplog.records if r.message == "access"]
    assert len(access_records) == 1
    assert access_records[0].path == "/chat"
    assert access_records[0].method == "POST"
    # duration_ms 是 int 且 >= 0
    assert isinstance(access_records[0].duration_ms, int)
    assert access_records[0].duration_ms >= 0


# ---------------------------------------------------------------------------
# request_id 在 contextvar 链路里能传到 handler
# ---------------------------------------------------------------------------


async def test_request_id_propagates_via_contextvars(
    fake_llm: _FakeLlm,
    fake_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中间件 set 的 request_id,handler 通过 ``request_id_var.get()`` 能读到。

    这个测试临时装一个 spy route 来读 contextvar。
    """
    monkeypatch.setenv("API_KEY", "test-key")
    app = create_app(
        llm_factory=lambda: fake_llm,
        config_loader=lambda: fake_config,
    )
    captured: dict[str, str | None] = {"from_var": None}

    @app.get("/__test_probe")
    async def probe() -> dict[str, str | None]:
        from logging_config import request_id_var

        captured["from_var"] = request_id_var.get()
        return {"rid": captured["from_var"]}

    incoming = "ctxvar-trace-abc"
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as ac,
    ):
        resp = await ac.get("/__test_probe", headers={"X-Request-ID": incoming})

    assert resp.status_code == 200
    assert captured["from_var"] == incoming
    assert resp.json()["rid"] == incoming
