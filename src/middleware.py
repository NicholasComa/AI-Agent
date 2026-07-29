"""Day 8 - 请求级中间件。

* :class:`RequestIdMiddleware` —— 注入 request_id 到 ``request.state``、
  :data:`logging_config.request_id_var`、响应头 ``X-Request-ID``。
* :class:`AccessLogMiddleware` —— 记录 method / path / status_code /
  duration_ms / request_id。绝不读请求体或 Authorization 头。

挂载顺序(在 :func:`src.app.create_app` 里)
-----------------------------------------

.. code-block:: python

    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(AccessLogMiddleware)

中间件按"后加先执行"工作,所以请求路径是::

    AccessLog → RequestId → route → RequestId → AccessLog

:class:`AccessLogMiddleware` 是最外层,负责计时整个请求;
:class:`RequestIdMiddleware` 是内层,负责在路由 handler 跑之前注入 request_id。

已知限制
--------

:class:`starlette.middleware.base.BaseHTTPMiddleware` 会缓存响应体,因此
与 SSE 流式响应不兼容 —— Day 9 的 ``/chat/stream`` 端点若直接通过
BaseHTTPMiddleware 走,可能出现 chunk 被合并 / 丢失。届时考虑切换到
纯 ASGI middleware。本中间件仅用于 Day 8 的非流式端点。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from logging_config import request_id_var

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
"""HTTP header 名(Starlette 会自动归一化为小写匹配)。"""


class RequestIdMiddleware(BaseHTTPMiddleware):
    """为每个请求注入 ``request_id``。

    来源优先级:

    1. 客户端请求头 ``X-Request-ID`` 非空 → 原样使用(分布式追踪透传);
    2. 否则生成 ``uuid.uuid4().hex``(32 字符 hex)。

    注入位置:

    * ``request.state.request_id`` —— 路由 handler 读取。
    * :data:`logging_config.request_id_var` —— 日志 filter 读取。
    * 响应头 ``X-Request-ID`` —— 客户端关联。

    ``request_id_var`` 用 ``ContextVar.set()`` / ``reset()`` 配对,确保
    请求结束后协程上下文不会被污染。
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        rid = incoming if incoming else uuid.uuid4().hex
        request.state.request_id = rid

        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)

        response.headers[REQUEST_ID_HEADER] = rid
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """访问日志,每个请求完成后记一行 INFO。

    通过 ``logger.info("access", extra=...)`` 输出,字段::

        method, path, status_code, duration_ms

    由 :class:`logging_config.JSONFormatter` 序列化为一行 JSON。
    ``request_id`` 由 :class:`logging_config.RequestIdFilter` 从
    :data:`logging_config.request_id_var` 自动注入。

    **绝不做**:

    * 不读 ``request.headers["authorization"]``。
    * 不读 ``request.body()``(body 是一次性的,读了路由就拿不到)。
    * 不记任何 query string(可能含密钥参数)。
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - start) * 1000)

        # 读 request.state.request_id(由 RequestIdMiddleware 写入,不随
        # ContextVar 的 reset 而丢失)。本中间件是外层,记日志时
        # request_id_var 已被内层 reset 回 None,所以必须从 state 取。
        # 通过 extra 透传,JSONFormatter 直接用,不再依赖 RequestIdFilter
        # 去读(已 reset 的) ContextVar。
        rid = getattr(request.state, "request_id", None)

        logger.info(
            "access",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "request_id": rid,
            },
        )
        return response


__all__ = ["AccessLogMiddleware", "REQUEST_ID_HEADER", "RequestIdMiddleware"]
