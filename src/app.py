"""Day 8 - FastAPI 应用工厂与 HTTP 接口。

本模块负责 **HTTP 层**。它只依赖项目内另外几个底层模块：

* :mod:`src.config` - 从环境变量 / JSON 加载 :class:`AppConfig`。
* :mod:`src.llm_client` - 带重试的异步对话补全客户端。
* :mod:`src.prompts` - 结构化输出的消息构造器。
* :mod:`src.schemas` - LLM 输出契约（:class:`RequirementAnalysis`）。
* :mod:`src.api_models` - HTTP 线协议契约。
* :mod:`src.logging_config` - JSON 日志 + request_id 注入（Day 8）。
* :mod:`src.middleware` - RequestId / AccessLog 中间件（Day 8）。

Day 8 变化
----------

* **JSON 结构化日志**:``create_app`` 启动时按 :class:`AppConfig` 的
  ``log_level`` / ``log_format`` 调 :func:`logging_config.configure_logging`。
* **Middleware 链路**:
  - :class:`RequestIdMiddleware` —— 注入 rid 到 ``request.state`` /
    :data:`logging_config.request_id_var` / 响应头。
  - :class:`AccessLogMiddleware` —— 记 ``method / path / status_code /
    duration_ms / request_id``，绝不读 body 或 Authorization。
* **新增端点**:``GET /models`` —— 返回当前模型 + ``extra_models`` 列表。
* **响应里携带 request_id**:``/chat`` / ``/analyze-requirement`` /
  ``/health`` / ``/models`` 的响应体多一个可选字段 ``request_id``，
  与响应头 ``X-Request-ID`` 同源。
* **/health 扩展**:顶层多 ``provider`` / ``max_concurrency`` / ``request_id``。

设计取舍
--------

1. **应用工厂**。``create_app()`` 返回一个新的 :class:`FastAPI`
   实例。测试用自定义的 fake ``llm_factory`` 构建自己的 app，从而
   完全不触碰网络。生产环境直接无参调用 ``create_app()``。
2. **由 lifespan 管理的客户端**。单个 :class:`LlmClient` 在启动时创建、
   关闭时销毁。各接口从 ``app.state.llm`` 取出它，从而保持简单。
3. **统一的错误信封**。每种已知异常类型都被映射到
   ``{"error": {"code", "message", "detail"}}`` 并带上合适的 HTTP 状态码。
   FastAPI 自动的 422 也被改写，客户端只需解析一种格式。
4. **任何响应都不含 API key**。``/health`` 只报告 ``key_configured``
   （一个布尔值），绝不返回 key 本身。错误消息也绝不回显请求体或请求头。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib import metadata
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api_models import (
    AnalyzeRequirementRequest,
    AnalyzeRequirementResponse,
    ChatRequest,
    ChatResponse,
    ErrorBody,
    ErrorResponse,
    HealthModelInfo,
    HealthResponse,
    ModelInfo,
    ModelsResponse,
)
from config import AppConfig, load_config
from llm_client import (
    LlmAuthError,
    LlmClient,
    LlmError,
    LlmRateLimitError,
    LlmResponseFormatError,
    LlmServerError,
    LlmTimeoutError,
)
from logging_config import configure_logging
from middleware import AccessLogMiddleware, RequestIdMiddleware
from prompts import build_messages

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 类型别名（工厂注入点）
# ---------------------------------------------------------------------------

LlmFactory = Callable[[], LlmClient]
"""一个无参可调用对象，产出就绪可用的 :class:`LlmClient`。"""

ConfigLoader = Callable[[], AppConfig]
"""一个无参可调用对象，返回校验过的 :class:`AppConfig`。"""


def _default_config_loader() -> AppConfig:
    """默认的 :class:`AppConfig` 加载器。

    强制 ``source="env"``（在入口中调用过 :func:`load_dotenv` 之后），
    这样当环境变量缺失时 API 服务会快速失败，而不是静默地沿用一份
    过期的 ``config.json``。
    """
    return load_config(source="env")


def _default_llm_factory() -> LlmClient:
    """默认的 :class:`LlmClient` 工厂。

    从环境变量读取 :class:`AppConfig` 并构造客户端，其 API key 取自
    ``API_KEY``。这里抛出的错误会冒泡到 FastAPI 的启动失败路径 -
    配置损坏时服务器会拒绝启动。
    """
    cfg = _default_config_loader()
    return LlmClient(
        base_url=cfg.api_base_url,
        model=cfg.model_name,
        timeout_seconds=cfg.timeout_seconds,
    )


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def create_app(
    *,
    llm_factory: LlmFactory | None = None,
    config_loader: ConfigLoader | None = None,
    version: str | None = None,
) -> FastAPI:
    """构建一个 :class:`FastAPI` 应用。

    Args:
        llm_factory: 单例 :class:`LlmClient` 的工厂。测试会注入一个
            fake 工厂以避免联网。``None`` 时使用 :func:`_default_llm_factory`。
        config_loader: :class:`AppConfig` 的工厂。``None`` 时使用
            :func:`_default_config_loader`。
        version: 包的版本字符串。``None`` 时从已安装分发的元数据读取。

    Returns:
        一个已注册全部四个接口的、配置好的 :class:`FastAPI` 实例。

    Note:
        Day 8 起 :func:`create_app` 会**提前**调一次 ``config_loader()``
        以配置 JSON 日志。如果配置不可用，会回退到 ``logging.basicConfig``
        + 一条 WARNING —— app 仍能启动，但日志降级为 plain 格式。
    """
    llm_factory = llm_factory or _default_llm_factory
    config_loader = config_loader or _default_config_loader
    if version is None:
        version = _resolve_version()

    # ---- 配置 JSON 日志（Day 8） ----
    # 提前调一次 config_loader 以拿到 log_level / log_format。
    # 失败时降级到 basicConfig,不阻塞 app 启动。
    try:
        cfg_for_logging = config_loader()
        configure_logging(cfg_for_logging.log_level, cfg_for_logging.log_format)
    except Exception as exc:  # noqa: BLE001 —— 配置失败是预期会发生的
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        logging.getLogger().warning(
            "failed to configure structured logging, falling back to plain: %s",
            exc,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """启动时创建 LlmClient，关闭时将其释放。"""
        llm = llm_factory()
        app.state.llm = llm
        try:
            yield
        finally:
            await llm.aclose()

    app = FastAPI(
        title="llm-gateway-demo",
        version=version,
        description=(
            "Week 02 模型网关：/health、/models、/chat、/analyze-requirement。"
            "基于 Day 3 的 LlmClient、Day 4 的结构化输出 schema、"
            "Day 6 Pydantic Settings、Day 7 ModelClient 协议、"
            "Day 8 Middleware + JSON 日志 构建。"
        ),
        lifespan=lifespan,
    )

    # Middleware 顺序：先 add RequestId（内层），再 add AccessLog（外层计时）。
    # Starlette "后加先执行"，所以最终请求路径是 AccessLog → RequestId → route。
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(AccessLogMiddleware)

    _register_exception_handlers(app)
    _register_routes(app, config_loader, version)

    return app


def _resolve_version() -> str:
    """读取包版本；未安装时回退到 ``"0.0.0"``。"""
    try:
        return metadata.version("llm-gateway-demo")
    except metadata.PackageNotFoundError:
        return "0.0.0"


# ---------------------------------------------------------------------------
# 异常处理器
# ---------------------------------------------------------------------------


def _register_exception_handlers(app: FastAPI) -> None:
    """把每种已知异常类型接到统一的错误信封上。"""

    @app.exception_handler(LlmAuthError)
    async def _auth_handler(_: Request, exc: LlmAuthError) -> JSONResponse:
        return _json_error(status.HTTP_401_UNAUTHORIZED, "llm_auth", str(exc))

    @app.exception_handler(LlmRateLimitError)
    async def _rate_handler(_: Request, exc: LlmRateLimitError) -> JSONResponse:
        return _json_error(status.HTTP_429_TOO_MANY_REQUESTS, "llm_rate_limit", str(exc))

    @app.exception_handler(LlmServerError)
    async def _server_handler(_: Request, exc: LlmServerError) -> JSONResponse:
        return _json_error(status.HTTP_502_BAD_GATEWAY, "llm_server", str(exc))

    @app.exception_handler(LlmTimeoutError)
    async def _timeout_handler(_: Request, exc: LlmTimeoutError) -> JSONResponse:
        return _json_error(status.HTTP_504_GATEWAY_TIMEOUT, "llm_timeout", str(exc))

    @app.exception_handler(LlmResponseFormatError)
    async def _format_handler(_: Request, exc: LlmResponseFormatError) -> JSONResponse:
        return _json_error(status.HTTP_502_BAD_GATEWAY, "llm_bad_response", str(exc))

    @app.exception_handler(LlmError)
    async def _llm_handler(_: Request, exc: LlmError) -> JSONResponse:
        # 兜底处理上面未覆盖的 LlmError 子类（例如来自 400/404 的原始 LlmError）。
        return _json_error(status.HTTP_502_BAD_GATEWAY, "llm_error", str(exc))

    @app.exception_handler(HTTPException)
    async def _http_handler(_: Request, exc: HTTPException) -> JSONResponse:
        # FastAPI 内置的 HTTPException，在接口内部抛出时触发
        # （例如 ``raise HTTPException(403)``）。路由层的 404 / 405
        # 由下面的状态码处理器接管。
        return _json_error(
            exc.status_code,
            _code_for_http_status(exc.status_code),
            str(exc.detail) if exc.detail else "",
        )

    # Starlette 的 ExceptionMiddleware 会先匹配状态码处理器，再匹配异常类处理器，
    # 因此路由层抛出的 ``HTTPException(404)`` 会绕过上面的 HTTPException 处理器，
    # 除非我们显式注册一个对应的状态码处理器。
    @app.exception_handler(404)
    async def _404_handler(_: Request, exc: Exception) -> JSONResponse:
        detail = getattr(exc, "detail", "") or "未找到请求的资源"
        return _json_error(404, "not_found", str(detail))

    @app.exception_handler(405)
    async def _405_handler(_: Request, exc: Exception) -> JSONResponse:
        detail = getattr(exc, "detail", "") or "该 URL 不支持此请求方法"
        return _json_error(405, "method_not_allowed", str(detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 422 - 改写成 ErrorResponse。我们保留 ``detail`` 中的结构化错误，
        # 方便测试按字段名做断言。
        return _json_error(
            422,
            "validation_error",
            "请求体校验失败",
            detail=json.dumps(exc.errors(), ensure_ascii=False),
        )

    @app.exception_handler(ValidationError)
    async def _pydantic_handler(_: Request, exc: ValidationError) -> JSONResponse:
        # 我们自己代码里抛出的 Pydantic ValidationError（例如当 LLM 返回的
        # JSON 无法解析成 RequirementAnalysis 时）。
        return _json_error(
            status.HTTP_502_BAD_GATEWAY,
            "llm_bad_response",
            "模型输出不符合 schema",
            detail=json.dumps(exc.errors(include_url=False), ensure_ascii=False),
        )

    @app.exception_handler(ValueError)
    async def _value_handler(_: Request, exc: ValueError) -> JSONResponse:
        # Pydantic 之外抛出的编程 / 客户端输入 ValueError（例如漏网的空消息列表）。
        # 按 400 处理。
        return _json_error(status.HTTP_400_BAD_REQUEST, "bad_request", str(exc))

    @app.exception_handler(Exception)
    async def _fallback_handler(_: Request, exc: Exception) -> JSONResponse:
        # 最后一道兜底处理器，确保客户端永远看到错误信封。
        logger.exception("API 处理器中未捕获的异常：%s", exc)
        return _json_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "发生未知错误",
            detail=type(exc).__name__,
        )


def _json_error(
    http_status: int,
    code: str,
    message: str,
    *,
    detail: str | None = None,
) -> JSONResponse:
    """构造一个带标准错误信封的 :class:`JSONResponse`。"""
    payload = ErrorResponse(
        error=ErrorBody(code=code, message=message, detail=detail, status_code=http_status)
    )
    return JSONResponse(status_code=http_status, content=payload.model_dump(mode="json"))


def _code_for_http_status(http_status: int) -> str:
    """把一个 :class:`HTTPException` 的状态码翻译成我们的错误码命名空间。"""
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        415: "unsupported_media_type",
        422: "validation_error",
        429: "rate_limited",
    }.get(http_status, "http_error")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def _register_routes(
    app: FastAPI,
    config_loader: ConfigLoader,
    version: str,
) -> None:
    """把全部四个接口挂到 ``app`` 上。"""

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        """存活探针 + 配置摘要。

        当 API key 已配置时返回 ``status="ok"``；否则返回
        ``status="degraded"``（服务器本身在线，但 ``/chat`` 和
        ``/analyze-requirement`` 会返回 401）。该接口从不触碰模型，
        因此即使上游挂了也能正常工作。

        Day 8 起额外携带 ``provider`` / ``max_concurrency`` / ``request_id``。
        """
        cfg = config_loader()
        key = os.environ.get("API_KEY", "")
        rid = getattr(request.state, "request_id", None)
        return HealthResponse(
            status="ok" if key else "degraded",
            version=version,
            model=HealthModelInfo(
                name=cfg.model_name,
                base_url=cfg.api_base_url,
                timeout_seconds=cfg.timeout_seconds,
                enable_stream=cfg.enable_stream,
                key_configured=bool(key),
                provider=cfg.provider,
                max_concurrency=cfg.max_concurrency,
            ),
            provider=cfg.provider,
            max_concurrency=cfg.max_concurrency,
            request_id=rid,
        )

    @app.get("/models", response_model=ModelsResponse)
    async def models(request: Request) -> ModelsResponse:
        """可用模型清单。

        返回当前默认模型 + ``extra_models`` 中列出的兜底模型。
        每条 :class:`ModelInfo` 含 ``key_configured`` 布尔（绝不暴露
        API key 本身）。当上游不可用时，客户端可以按此列表回退。
        """
        cfg = config_loader()
        key = os.environ.get("API_KEY", "")
        rid = getattr(request.state, "request_id", None)
        items = [
            ModelInfo(
                name=cfg.model_name,
                base_url=cfg.api_base_url,
                provider=cfg.provider,
                key_configured=bool(key),
            )
        ]
        for name in cfg.extra_models:
            # 兜底模型可能用同一个 base_url；先实现最朴素版本 —— Day 9
            # 若要支持多 base_url,扩展 AppConfig。
            items.append(
                ModelInfo(
                    name=name,
                    base_url=cfg.api_base_url,
                    provider=cfg.provider,
                    key_configured=bool(key),
                )
            )
        return ModelsResponse(models=items, current=cfg.model_name, request_id=rid)

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest, request: Request) -> ChatResponse:
        """普通对话补全代理。

        请求体：:class:`ChatRequest`。响应体：:class:`ChatResponse`。
        Day 8 起响应里多一个 ``request_id``。
        """
        llm: LlmClient = request.app.state.llm
        messages: list[dict[str, str]] = [
            {"role": m.role, "content": m.content} for m in req.messages
        ]
        extra_body: dict[str, Any] = {}
        if req.temperature is not None:
            extra_body["temperature"] = req.temperature
        if req.max_tokens is not None:
            extra_body["max_tokens"] = req.max_tokens

        result = await llm.chat(
            messages,
            model=req.model,
            extra_body=extra_body or None,
        )
        rid = getattr(request.state, "request_id", None)
        return ChatResponse(
            text=result.text,
            model=result.model,
            elapsed_ms=result.elapsed_ms,
            usage=result.usage,
            request_id=rid,
        )

    @app.post("/analyze-requirement", response_model=AnalyzeRequirementResponse)
    async def analyze_requirement(
        req: AnalyzeRequirementRequest,
        request: Request,
    ) -> AnalyzeRequirementResponse:
        """结构化需求分析。

        请求体：:class:`AnalyzeRequirementRequest`。
        响应体：:class:`AnalyzeRequirementResponse`（继承
        :class:`src.schemas.RequirementAnalysis`）。

        该接口强制 ``response_format=json_object``，使 OpenAI 兼容的
        供应商（OpenAI / DeepSeek / Ollama / vLLM）都能进入 JSON 模式。
        随后把输出文本按 :class:`RequirementAnalysis` 做严格校验；
        任何结构不符都会变成带 ``llm_bad_response`` 码的 502。
        Day 8 起响应里多一个 ``request_id``。
        """
        llm: LlmClient = request.app.state.llm
        messages = build_messages(req.text)
        extra_body: dict[str, Any] = {"response_format": {"type": "json_object"}}

        result = await llm.chat(messages, extra_body=extra_body)

        # 严格解析：任何与 RequirementAnalysis 的偏差都会抛 ValidationError,
        # 由全局处理器映射为 502。
        parsed = AnalyzeRequirementResponse.model_validate_json(result.text)
        rid = getattr(request.state, "request_id", None)
        # model_validate_json 不接收额外 kwargs;手动赋值 request_id
        parsed.request_id = rid
        return parsed


# ---------------------------------------------------------------------------
# 供 ``fastapi dev src/app.py`` 使用的惰性 app 实例
# ---------------------------------------------------------------------------

# ``fastapi dev`` 与 ``fastapi run`` 会导入本模块并寻找名为 ``app`` 的属性。
# 我们在此暴露一个用默认工厂构建的模块级 ``app``，这样 Day 5 规范里的命令
# （``uv run fastapi dev src/main.py``）无论通过本文件还是通过专门的
# ``src/main.py`` 入口都能工作 - 二者结果一致，因为后者也是自行调用
# ``create_app()``。

app = create_app()


__all__ = [
    "ConfigLoader",
    "LlmFactory",
    "app",
    "create_app",
]
