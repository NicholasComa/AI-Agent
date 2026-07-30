"""Day 8 - 请求级中间件；Day 9 增补纯 ASGI 版本以兼容 SSE。

* :class:`RequestIdMiddleware` —— 注入 request_id 到 ``request.state``、
  :data:`logging_config.request_id_var`、响应头 ``X-Request-ID``。
* :class:`AccessLogMiddleware` —— 记录 method / path / status_code /
  duration_ms / request_id。绝不读请求体或 Authorization 头。
* :class:`RequestIdASGIMiddleware` / :class:`AccessLogASGIMiddleware` ——
  纯 ASGI 版本，Day 9 起替换上面的 BaseHTTPMiddleware 版本。

挂载顺序(在 :func:`src.app.create_app` 里)
-----------------------------------------

.. code-block:: python

    app = RequestIdASGIMiddleware(app)  # 包裹整个 FastAPI app
    app.add_middleware(AccessLogMiddleware)  # 外层计时

中间件按"后加先执行"工作,所以请求路径是::

    AccessLog → RequestId → route → RequestId → AccessLog

:func:`create_app` 把 :class:`RequestIdASGIMiddleware` 用"包外层"方式
而不是 ``add_middleware`` 挂载,原因见下"已知限制"。

已知限制(为什么有 ASGI 版本)
---------------------------

:class:`starlette.middleware.base.BaseHTTPMiddleware` 会缓存响应体,因此
**与 SSE 流式响应不兼容** —— Day 9 的 ``/chat/stream`` 端点若直接通过
BaseHTTPMiddleware 走,chunk 会被合并,极端情况下还会丢帧。

解决:把 :class:`RequestIdMiddleware` 替换为 :class:`RequestIdASGIMiddleware`
(纯 ASGI 协议,不缓存 body)。:class:`AccessLogMiddleware` 仍是
BaseHTTPMiddleware —— 它是**最外层**,只需要 ``http.response.start`` 里
的状态码,不需要响应体,所以缓存对它没影响。功能上完全等价。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from logging_config import request_id_var

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
"""HTTP header 名(Starlette 会自动归一化为小写匹配)。"""


# ---------------------------------------------------------------------------
# 兼容层:Day 8 的 BaseHTTPMiddleware 版本
# ---------------------------------------------------------------------------


class RequestIdMiddleware(BaseHTTPMiddleware):
    """为每个请求注入 ``request_id`` (BaseHTTPMiddleware 版本)。

    .. deprecated::
        Day 9 起请改用 :class:`RequestIdASGIMiddleware`。本类保留仅为
        给可能直接 ``add_middleware`` 它的旧代码兜底;Day 10 之后的清理
        阶段会移除。

    行为:

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
    """访问日志,每个请求完成后记一行 INFO (BaseHTTPMiddleware 版本)。

    .. deprecated::
        如果你的项目用 SSE,改用 :class:`AccessLogASGIMiddleware`。本类
        仍可安全地作为**最外层**计时(它只读 ``http.response.start`` 消息
        拿状态码,不消费响应体),因此在当前架构里被保留。

    通过 ``logger.info("access", extra=...)`` 输出,字段::

        method, path, status_code, duration_ms, request_id

    绝不读 ``Authorization`` 头、请求体、query string。
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - start) * 1000)

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


# ---------------------------------------------------------------------------
# Day 9 - 纯 ASGI 版本
# ---------------------------------------------------------------------------


class RequestIdASGIMiddleware:
    """为每个 HTTP 请求注入 ``request_id`` (纯 ASGI,不缓存 body)。

    与 :class:`RequestIdMiddleware` 的对外行为完全一致,但实现走纯 ASGI
    协议,因此**与 SSE / 流式响应兼容**。

    行为:

    1. 客户端请求头 ``x-request-id`` 非空 → 原样使用;
    2. 否则生成 ``uuid.uuid4().hex``(32 字符 hex)。

    注入位置:

    * ``scope["state"]["request_id"]`` —— :class:`fastapi.Request.state`
      的最终实现就是读这里,所以路由 handler 里 ``request.state.request_id``
      仍然能拿到。
    * :data:`logging_config.request_id_var` —— 日志 filter 读取。
    * 响应头 ``x-request-id`` —— 在 ``http.response.start`` 消息里追加。

    包裹方式(不是 ``add_middleware``)::

        app = RequestIdASGIMiddleware(app)
        # or:
        app = FastAPI(...)
        app = RequestIdASGIMiddleware(app)
    """

    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            # lifespan / websocket 等:不注入
            await self.app(scope, receive, send)
            return

        # 1. 解析 rid
        rid: str | None = None
        for k, v in scope.get("headers", []):
            # ASGI 头是小写 bytes
            if k == b"x-request-id":
                decoded = v.decode("latin-1").strip()
                if decoded:
                    rid = decoded
                break
        if not rid:
            rid = uuid.uuid4().hex

        # 2. 注入到 scope.state(给 FastAPI request.state 用)
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["request_id"] = rid

        # 3. 注入到 ContextVar(给日志 filter 用)
        token = request_id_var.set(rid)

        # 4. 包装 send,在 response.start 里追加响应头
        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                # 移除已有的 x-request-id(避免重复)
                headers = [(k, v) for k, v in headers if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", rid.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)


class AccessLogASGIMiddleware:
    """访问日志 (纯 ASGI 版本,与 SSE 兼容)。

    与 :class:`AccessLogMiddleware` 行为一致:每个 HTTP 请求完成后记一行
    INFO,通过 ``logger.info("access", extra=...)`` 输出,字段::

        method, path, status_code, duration_ms, request_id

    从 :data:`logging_config.request_id_var` 读 request_id —— 由内层
    :class:`RequestIdASGIMiddleware` 注入,日志时仍处于 set 状态(我们
    在内层 reset 之前就发完日志)。

    绝不读 ``Authorization`` 头、请求体、query string。
    """

    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = 500  # 兜底:如果下面没收到 response.start

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            # 内层 RequestIdASGIMiddleware 还在 set 着;我们读它
            rid = request_id_var.get()
            logger.info(
                "access",
                extra={
                    "method": scope.get("method", "-"),
                    "path": scope.get("path", "-"),
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                    "request_id": rid,
                },
            )


__all__ = [
    "AccessLogASGIMiddleware",
    "AccessLogMiddleware",
    "REQUEST_ID_HEADER",
    "RequestIdASGIMiddleware",
    "RequestIdMiddleware",
]
