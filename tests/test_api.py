"""Day 5 FastAPI 服务（:mod:`src.app` + :mod:`src.api_models`）的测试。

测试策略
--------

所有测试都通过 ``httpx.ASGITransport`` 在进程内针对 ASGI app 运行 -
没有真实的 socket，也没有真实的模型。每个测试都用自定义的 ``llm_factory``
构建自己的 app 实例，从而可以：

* 在成功路径上返回写死的 :class:`LlmResult` 对象，
* 在错误路径测试上抛出特定的 :class:`LlmError` 子类，
* 替换 :class:`AppConfig` 来验证 ``key_configured`` 标记。

我们通过 ``app.router.lifespan_context(app)`` 手动驱动 lifespan，
使 ``app.state.llm`` 的填充与生产环境完全一致。这意味着每个测试都
会真实跑一遍启动 / 关闭流程 - 包括 fake 客户端的 ``aclose()``。

覆盖的接口：

* ``GET  /health``                 - 正常 / 降级 / 未知路由
* ``POST /chat``                   - 正常路径 + 422 / 401 / 502 / 504
* ``POST /analyze-requirement``    - 正常路径 + 422 / 502

错误永远使用 ``ErrorResponse`` 信封；所有针对错误形状的断言都通过
``response.json()["error"]`` 进行。
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from api_models import ErrorResponse
from app import create_app
from config import AppConfig
from llm_client import (
    LlmAuthError,
    LlmRateLimitError,
    LlmResponseFormatError,
    LlmResult,
    LlmServerError,
    LlmTimeoutError,
)

# ---------------------------------------------------------------------------
# 测试替身（Fake）
# ---------------------------------------------------------------------------


class _FakeLlm:
    """用于替代 :class:`LlmClient` 的测试替身。

    ``chat`` 的行为由通过 ``set_responder`` 设置的单个可调用对象控制。
    每个测试安装它想要的 responder。

    Attributes:
        aclose_count: :meth:`aclose` 被调用了多少次。lifespan 关闭测试
            会断言它恰好为 1。
    """

    def __init__(self) -> None:
        self._responder: Callable[..., Any] = self._default_responder
        self.aclose_count = 0
        self.received_calls: list[dict[str, Any]] = []

    def set_responder(self, responder: Callable[..., Any]) -> None:
        self._responder = responder

    async def _default_responder(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> LlmResult:
        return LlmResult(
            text="hello from fake",
            model="fake-model",
            elapsed_ms=12,
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        )

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
        result: LlmResult | Awaitable[LlmResult] = self._responder(
            messages, model=model, extra_body=extra_body
        )
        # responder 可能是一个普通函数（直接返回 LlmResult）或
        # 一个 ``async def`` / 协程；只在需要时 await，让测试保持简洁。
        if inspect.isawaitable(result):
            return await result
        return result

    async def aclose(self) -> None:
        self.aclose_count += 1

    async def __aenter__(self) -> _FakeLlm:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # 下面的属性镜像真实 LlmClient 的对外接口（部分接口在打日志时会读取它们）。
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
    """测试用的静态 :class:`AppConfig`。"""
    return AppConfig(
        model_name="fake-model",
        api_base_url="http://fake/v1",
        timeout_seconds=1.0,
        enable_stream=False,
    )


# ---------------------------------------------------------------------------
# 测试夹具（Fixture）
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
    """构建一个注入了 fake 的全新 app，并产出一个 httpx 客户端。

    每个测试都拿到自己的 app，因此 responder 状态不会在测试之间泄漏。
    ``monkeypatch.setenv`` 默认让 ``/health`` 接口看到一个已配置的 API key；
    想要 ``degraded`` 的测试自行把它清除。
    """
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
# /health
# ---------------------------------------------------------------------------


async def test_health_ok(client: httpx.AsyncClient) -> None:
    """``/health`` 返回 200，status=ok，以及 version + 模型摘要。"""
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]
    assert body["model"]["name"] == "fake-model"
    assert body["model"]["base_url"] == "http://fake/v1"
    assert body["model"]["timeout_seconds"] == 1.0
    assert body["model"]["enable_stream"] is False
    assert body["model"]["key_configured"] is True


async def test_health_degraded_when_no_key(
    monkeypatch: pytest.MonkeyPatch,
    fake_llm: _FakeLlm,
    fake_config: AppConfig,
) -> None:
    """当环境变量中的 key 为空时，``/health`` 报告 ``degraded`` 且 ``key_configured=False``。"""
    monkeypatch.delenv("API_KEY", raising=False)
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
        resp = await ac.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["model"]["key_configured"] is False


async def test_unknown_route_returns_404_envelope(client: httpx.AsyncClient) -> None:
    """未知路由仍会返回 ErrorResponse 信封（经由 HTTPException）。"""
    resp = await client.get("/no-such-endpoint")
    assert resp.status_code == 404
    parsed = ErrorResponse.model_validate(resp.json())
    assert parsed.error.code == "not_found"


# ---------------------------------------------------------------------------
# /chat - 正常路径
# ---------------------------------------------------------------------------


async def test_chat_happy_path(client: httpx.AsyncClient, fake_llm: _FakeLlm) -> None:
    """成功的对话：响应字段被填充，fake 收到了消息。"""
    resp = await client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "hello from fake"
    assert body["model"] == "fake-model"
    assert body["elapsed_ms"] == 12
    assert body["usage"] == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }

    # fake 应当被调用一次，且传入的消息列表完全一致。
    assert len(fake_llm.received_calls) == 1
    call = fake_llm.received_calls[0]
    assert call["messages"] == [{"role": "user", "content": "hi"}]
    # 请求体中没有 temperature / max_tokens => extra_body 中也不应有覆盖值。
    assert call["extra_body"] is None


async def test_chat_passes_temperature_and_max_tokens(
    client: httpx.AsyncClient, fake_llm: _FakeLlm
) -> None:
    """可选字段一旦设置，就通过 ``extra_body`` 转发。"""
    resp = await client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2,
            "max_tokens": 64,
            "model": "override-model",
        },
    )
    assert resp.status_code == 200, resp.text
    call = fake_llm.received_calls[0]
    assert call["model"] == "override-model"
    assert call["extra_body"] == {"temperature": 0.2, "max_tokens": 64}


# ---------------------------------------------------------------------------
# /chat - 校验
# ---------------------------------------------------------------------------


async def test_chat_empty_messages_returns_422(client: httpx.AsyncClient) -> None:
    """空消息列表 -> 422，code=validation_error。"""
    resp = await client.post("/chat", json={"messages": []})
    assert resp.status_code == 422
    parsed = ErrorResponse.model_validate(resp.json())
    assert parsed.error.code == "validation_error"
    assert "messages" in parsed.error.detail  # detail 中包含了 pydantic 的结构化错误


async def test_chat_bad_temperature_returns_422(client: httpx.AsyncClient) -> None:
    """超出范围的 temperature 被 Pydantic 拒绝，而非被模型拒绝。"""
    resp = await client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 5.0,
        },
    )
    assert resp.status_code == 422
    parsed = ErrorResponse.model_validate(resp.json())
    assert parsed.error.code == "validation_error"


async def test_chat_unknown_role_returns_422(client: httpx.AsyncClient) -> None:
    """``role`` 不在字面量范围内时在边界被拒绝。"""
    resp = await client.post(
        "/chat",
        json={
            "messages": [{"role": "tool", "content": "x"}],
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /chat - 上游错误映射
# ---------------------------------------------------------------------------


async def test_chat_auth_error_returns_401(client: httpx.AsyncClient, fake_llm: _FakeLlm) -> None:
    """鉴权失败 -> 401，code=llm_auth。"""
    fake_llm.set_responder(lambda *_a, **_kw: (_ for _ in ()).throw(LlmAuthError("bad key")))
    resp = await client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401
    parsed = ErrorResponse.model_validate(resp.json())
    assert parsed.error.code == "llm_auth"


async def test_chat_rate_limit_returns_429(client: httpx.AsyncClient, fake_llm: _FakeLlm) -> None:
    """限流 -> 429，code=llm_rate_limit。"""
    fake_llm.set_responder(lambda *_a, **_kw: (_ for _ in ()).throw(LlmRateLimitError("slow down")))
    resp = await client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 429
    parsed = ErrorResponse.model_validate(resp.json())
    assert parsed.error.code == "llm_rate_limit"


async def test_chat_server_error_returns_502(client: httpx.AsyncClient, fake_llm: _FakeLlm) -> None:
    """上游 5xx -> 502，code=llm_server。"""
    fake_llm.set_responder(lambda *_a, **_kw: (_ for _ in ()).throw(LlmServerError("upstream 500")))
    resp = await client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 502
    parsed = ErrorResponse.model_validate(resp.json())
    assert parsed.error.code == "llm_server"


async def test_chat_timeout_returns_504(client: httpx.AsyncClient, fake_llm: _FakeLlm) -> None:
    """超时 -> 504，code=llm_timeout。"""
    fake_llm.set_responder(
        lambda *_a, **_kw: (_ for _ in ()).throw(LlmTimeoutError("took too long"))
    )
    resp = await client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 504
    parsed = ErrorResponse.model_validate(resp.json())
    assert parsed.error.code == "llm_timeout"


async def test_chat_response_format_error_returns_502(
    client: httpx.AsyncClient, fake_llm: _FakeLlm
) -> None:
    """响应格式错误 -> 502，code=llm_bad_response。"""
    fake_llm.set_responder(
        lambda *_a, **_kw: (_ for _ in ()).throw(LlmResponseFormatError("not json"))
    )
    resp = await client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 502
    parsed = ErrorResponse.model_validate(resp.json())
    assert parsed.error.code == "llm_bad_response"


# ---------------------------------------------------------------------------
# /analyze-requirement - 正常路径
# ---------------------------------------------------------------------------


async def test_analyze_requirement_happy_path(
    client: httpx.AsyncClient, fake_llm: _FakeLlm
) -> None:
    """成功的分析：LLM 返回合法 JSON，响应符合 schema。"""

    def responder(
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> LlmResult:
        payload = {
            "title": "电商网站",
            "category": "web",
            "functional_points": ["登录", "浏览", "支付"],
            "risks": ["支付合规"],
            "clarification_questions": ["支付方式？"],
            "confidence": 0.85,
        }
        assert extra_body == {"response_format": {"type": "json_object"}}
        return LlmResult(
            text=json.dumps(payload, ensure_ascii=False),
            model="fake-model",
            elapsed_ms=42,
        )

    fake_llm.set_responder(responder)

    resp = await client.post(
        "/analyze-requirement",
        json={"text": "做一个电商网站，登录+商品浏览+支付"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "电商网站"
    assert body["category"] == "web"
    assert body["confidence"] == 0.85
    assert body["functional_points"] == ["登录", "浏览", "支付"]


async def test_analyze_requirement_invalid_json_returns_502(
    client: httpx.AsyncClient, fake_llm: _FakeLlm
) -> None:
    """当 LLM 返回非 JSON 时，我们返回 502 llm_bad_response。"""
    fake_llm.set_responder(
        lambda *_a, **_kw: LlmResult(
            text="not json at all",
            model="fake-model",
            elapsed_ms=10,
        )
    )
    resp = await client.post(
        "/analyze-requirement",
        json={"text": "something"},
    )
    assert resp.status_code == 502
    parsed = ErrorResponse.model_validate(resp.json())
    assert parsed.error.code == "llm_bad_response"


async def test_analyze_requirement_schema_mismatch_returns_502(
    client: httpx.AsyncClient, fake_llm: _FakeLlm
) -> None:
    """JSON 能解析但不符合 RequirementAnalysis -> 502（Pydantic 处理器）。"""
    fake_llm.set_responder(
        lambda *_a, **_kw: LlmResult(
            text=json.dumps({"title": "ok", "category": "web"}),  # 缺少必填字段
            model="fake-model",
            elapsed_ms=10,
        )
    )
    resp = await client.post(
        "/analyze-requirement",
        json={"text": "do a thing"},
    )
    assert resp.status_code == 502
    parsed = ErrorResponse.model_validate(resp.json())
    assert parsed.error.code == "llm_bad_response"


async def test_analyze_requirement_short_text_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """``text`` 短于 min_length 时在边界被拒绝。"""
    resp = await client.post("/analyze-requirement", json={"text": "x"})
    assert resp.status_code == 422
    parsed = ErrorResponse.model_validate(resp.json())
    assert parsed.error.code == "validation_error"


async def test_analyze_requirement_timeout_returns_504(
    client: httpx.AsyncClient, fake_llm: _FakeLlm
) -> None:
    """超时 -> 504，code=llm_timeout。"""
    fake_llm.set_responder(lambda *_a, **_kw: (_ for _ in ()).throw(LlmTimeoutError("oops")))
    resp = await client.post(
        "/analyze-requirement",
        json={"text": "做点什么"},
    )
    assert resp.status_code == 504
    parsed = ErrorResponse.model_validate(resp.json())
    assert parsed.error.code == "llm_timeout"


# ---------------------------------------------------------------------------
# 横切关注点
# ---------------------------------------------------------------------------


async def test_lifespan_closes_the_fake_client(
    fake_llm: _FakeLlm,
    fake_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退出 lifespan 时必须恰好关闭 LlmClient 一次。"""
    monkeypatch.setenv("API_KEY", "test-key")
    app = create_app(
        llm_factory=lambda: fake_llm,
        config_loader=lambda: fake_config,
    )
    async with app.router.lifespan_context(app):
        assert fake_llm.aclose_count == 0
    assert fake_llm.aclose_count == 1


async def test_create_app_default_factory_works() -> None:
    """无参调用 ``create_app()`` 返回一个可用的 FastAPI。

    我们并不启动 app（那会尝试连接真实 LLM）；只断言 ``create_app()``
    可被调用并返回一个注册了期望路由的 FastAPI 实例。
    """
    app: FastAPI = create_app()
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/health" in paths
    assert "/chat" in paths
    assert "/analyze-requirement" in paths
