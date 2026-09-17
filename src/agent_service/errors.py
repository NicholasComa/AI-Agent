"""服务层错误码与统一错误信封。

对外只暴露一种错误响应形状，与 Week 02 网关（``src/app.py``）保持一致:

    {
        "error": {
            "code": "...",
            "message": "...",
            "detail": "...",
            "status_code": 503,
            "request_id": "...",
        }
    }

信封模型直接复用 :class:`api_models.ErrorBody` / :class:`api_models.ErrorResponse`，
因此客户端只需要一套解析逻辑。

业务代码抛出 :class:`ServiceError`，由应用层注册的异常处理器转成响应；
错误码到 HTTP 状态码的映射集中在 :data:`DEFAULT_STATUS`，避免各处硬编码。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from api_models import ErrorBody, ErrorResponse


class ErrorCode(StrEnum):
    """服务对外错误码。

    Attributes:
        INVALID_ARGUMENT: 参数校验失败。
        UNAUTHORIZED: 认证失败（密钥缺失或不匹配）。
        RATE_LIMITED: 超出配额，或并发闸门排队超时。
        PAYLOAD_TOO_LARGE: 请求体超过大小上限。
        DEPENDENCY_UNAVAILABLE: 依赖不可用（Qdrant / MCP / 模型接口）。
        TIMEOUT: 单次请求处理超时。
        CANCELLED: 客户端主动断开。只用于日志归类，不构成响应。
        INTERNAL: 未预期的服务端错误。
    """

    INVALID_ARGUMENT = "invalid_argument"
    UNAUTHORIZED = "unauthorized"
    RATE_LIMITED = "rate_limited"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


DEFAULT_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_ARGUMENT: 422,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.TIMEOUT: 504,
    # 499 是 nginx 的非标准约定，仅用于把「客户端主动断开」归类到统一错误码，
    # 不会作为响应状态码返回给客户端。
    ErrorCode.CANCELLED: 499,
    ErrorCode.INTERNAL: 500,
}
"""错误码到 HTTP 状态码的缺省映射。"""


def get_request_id(request: Request) -> str | None:
    """读取当前请求的 ``request_id``。

    ``request_id`` 由 :class:`middleware.RequestIdASGIMiddleware` 写入
    ``scope["state"]``；中间件缺失时返回 ``None`` 而不是抛异常，保证错误
    处理路径本身不会二次失败。
    """
    return getattr(request.state, "request_id", None)


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    detail: str | None = None,
    request_id: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """构造带统一信封的错误响应。

    Args:
        status_code: HTTP 状态码。
        code: 机器可读错误码。
        message: 人类可读的一行描述；不得包含密钥或请求体内容。
        detail: 可选的调试细节。
        request_id: 与响应头 ``X-Request-ID`` 同源，便于关联日志。
        headers: 附加响应头，例如限流时的 ``Retry-After``。
    """
    payload = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            detail=detail,
            status_code=status_code,
            request_id=request_id,
        )
    )
    response = JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))
    if headers:
        response.headers.update(headers)
    return response


class ServiceError(Exception):
    """服务层可预期异常。

    Attributes:
        code: 对外错误码，决定缺省 HTTP 状态码。
        message: 面向客户端的一行描述。
        status_code: HTTP 状态码；缺省取 :data:`DEFAULT_STATUS`。
        detail: 可选的调试细节，不得包含密钥。
        headers: 附加响应头；缺省为空。限流用它下发 ``Retry-After``。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code if status_code is not None else DEFAULT_STATUS[code]
        self.detail = detail
        self.headers: dict[str, str] = dict(headers) if headers else {}

    def to_response(self, request_id: str | None = None) -> JSONResponse:
        """把本异常转成统一信封响应。"""
        return error_response(
            self.status_code,
            self.code.value,
            self.message,
            detail=self.detail,
            request_id=request_id,
            headers=self.headers or None,
        )


def status_code_to_error_code(status_code: int) -> ErrorCode:
    """把 FastAPI / Starlette 抛出的 HTTP 状态码映射到服务错误码。"""
    if status_code in (401, 403):
        return ErrorCode.UNAUTHORIZED
    if status_code == 413:
        return ErrorCode.PAYLOAD_TOO_LARGE
    if status_code == 429:
        return ErrorCode.RATE_LIMITED
    if status_code == 504:
        return ErrorCode.TIMEOUT
    if status_code in (400, 404, 405, 422):
        return ErrorCode.INVALID_ARGUMENT
    if status_code == 503:
        return ErrorCode.DEPENDENCY_UNAVAILABLE
    return ErrorCode.INTERNAL


def error_detail_from_exception(exc: BaseException) -> str:
    """生成只含类型与消息的调试细节，不携带请求体或密钥。"""
    text: Any = str(exc) or exc.__class__.__name__
    return f"{type(exc).__name__}: {text}"
