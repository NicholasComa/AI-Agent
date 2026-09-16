"""Agent 服务应用工厂。

把 RAG 检索、LangGraph 工作流与 MCP 工具统一挂到一个 FastAPI 应用上，
中间件、异常处理器与错误信封沿用 Week 02 网关的实现，保证两个应用的
对外行为一致。

运行方式::

    uv run python scripts/serve.py --app src.agent_service.app:app --port 8080

容器内可以直接用 uvicorn 拉起同一个导入路径::

    python -m uvicorn src.agent_service.app:app --host 0.0.0.0 --port 8080

模块底部的 ``app`` 由 :func:`create_agent_service_app` 构建，只为满足
ASGI 服务器「导入模块并寻找 ``app`` 属性」的约定；真正的依赖组装推迟到
生命周期钩子里执行，因此导入本模块不会建立任何网络连接。
"""

from __future__ import annotations

import logging
from importlib import metadata

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from middleware import AccessLogASGIMiddleware, RequestIdASGIMiddleware

from .deps import AgentServiceDeps
from .errors import (
    ErrorCode,
    ServiceError,
    error_detail_from_exception,
    error_response,
    get_request_id,
    status_code_to_error_code,
)
from .lifespan import UNKNOWN_VERSION, service_lifespan
from .routes import health_router, rag_router, tools_router, workflow_router

logger = logging.getLogger(__name__)

_DISTRIBUTION_NAME = "llm-gateway-demo"
_HTTP_UNPROCESSABLE = 422
_HTTP_INTERNAL = 500
_DESCRIPTION = (
    "Agent 服务：把 RAG 检索、LangGraph 工作流与 MCP 工具组合为可运行、"
    "可诊断、可部署的后端服务。提供存活探针、就绪探针、指标摘要，"
    "以及 RAG 问答、需求分析工作流与 MCP 工具调用三组业务接口。"
)


def _resolve_version() -> str:
    """读取包版本；未安装分发时回退到 ``UNKNOWN_VERSION``。"""
    try:
        return metadata.version(_DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        return UNKNOWN_VERSION


def create_agent_service_app(
    *,
    deps: AgentServiceDeps | None = None,
    version: str | None = None,
    title: str = "jwipc-agent-service",
) -> FastAPI:
    """构建 Agent 服务应用。

    Args:
        deps: 预置的依赖容器。测试注入替身时传入，此时生命周期钩子不再
            重新组装；为 ``None`` 时由生命周期钩子按环境变量组装。
        version: 应用版本；缺省从已安装分发的元数据读取。
        title: 应用标题，出现在 OpenAPI 文档中。

    Returns:
        注册了中间件、异常处理器与全部路由的 :class:`FastAPI` 实例。
    """
    resolved_version = version or _resolve_version()

    app = FastAPI(
        title=title,
        version=resolved_version,
        description=_DESCRIPTION,
        lifespan=service_lifespan,
    )
    if deps is not None:
        app.state.deps = deps

    # 两个中间件都是纯 ASGI 实现，不缓存响应体，因此与 SSE 流式端点兼容。
    # 后添加的处于外层，最终请求路径为 RequestId -> AccessLog -> 路由。
    app.add_middleware(AccessLogASGIMiddleware)
    app.add_middleware(RequestIdASGIMiddleware)

    _register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(rag_router)
    app.include_router(workflow_router)
    app.include_router(tools_router)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """把各类异常统一收敛到同一个错误信封。"""

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return exc.to_response(get_request_id(request))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        location = ".".join(str(part) for part in first.get("loc", ()))
        message = f"invalid request: {location} {first.get('msg', '')}".strip()
        return error_response(
            _HTTP_UNPROCESSABLE,
            ErrorCode.INVALID_ARGUMENT.value,
            message,
            detail=f"{len(errors)} validation error(s)",
            request_id=get_request_id(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = status_code_to_error_code(exc.status_code)
        return error_response(
            exc.status_code,
            code.value,
            str(exc.detail),
            request_id=get_request_id(request),
        )

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        # 详细信息只进日志；响应体只保留异常类型，避免把内部信息暴露给客户端。
        logger.error(
            "unhandled error path=%s detail=%s",
            request.url.path,
            error_detail_from_exception(exc),
        )
        return error_response(
            _HTTP_INTERNAL,
            ErrorCode.INTERNAL.value,
            "internal server error",
            detail=type(exc).__name__,
            request_id=get_request_id(request),
        )


app = create_agent_service_app()
"""供 ASGI 服务器定位的模块级应用实例。"""
