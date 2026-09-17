"""服务安全加固：Bearer 认证、令牌桶限流与请求体大小限制。

三种手段分处不同层次，各自拦截自己能最早发现的异常：

- **Bearer 认证**用 FastAPI 依赖实现，挂在业务路由上，因此 ``/health`` 与
  ``/ready`` 天然豁免——探针要能在依赖故障时照常回答。
- **限流**同样是路由依赖，按客户端标识扣令牌，超配额返回 429 并带
  ``Retry-After``，客户端可据此退避而不是盲目重试。
- **请求体大小限制**必须早于路由层解析，因此用纯 ASGI 中间件实现：先看
  ``content-length``，声明值超限就直接拒绝、不读请求体；未声明长度（分块
  传输）时在读取过程中累计字节数，超限即中断。

三者都复用 :mod:`agent_service.errors` 的统一错误信封，客户端只需一套解析
逻辑。认证与限流都不回显密钥，也不把请求体内容写进响应或日志。
"""

from __future__ import annotations

import logging
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from .deps import AgentServiceDeps, ServiceDeps
from .errors import ErrorCode, ServiceError, error_response

logger = logging.getLogger(__name__)

AUTHORIZATION_HEADER = "authorization"
"""请求头名；Starlette 已把 header 名归一化为小写。"""

CLIENT_ID_HEADER = "x-client-id"
"""客户端自报标识，用于把限流配额分到不同调用方。"""

RETRY_AFTER_HEADER = "Retry-After"
"""429 响应携带的建议退避秒数。"""

BEARER_PREFIX = "bearer "
"""``Authorization`` 头的方案前缀，比较时大小写不敏感。"""

DEFAULT_MAX_BODY_BYTES = 262_144
"""取不到配置时的请求体上限，与 :class:`AgentServiceSettings` 缺省值一致。"""

RATE_LIMIT_WINDOW_SECONDS = 60.0
"""配额窗口：配置按「每分钟」给出，补充速率由此换算。"""


# ---------------------------------------------------------------------------
# Bearer 认证
# ---------------------------------------------------------------------------


def _bearer_token(header_value: str | None) -> str | None:
    """从 ``Authorization`` 头里取出令牌，格式不符时返回 ``None``。"""
    if not header_value:
        return None
    text = header_value.strip()
    if text[: len(BEARER_PREFIX)].lower() != BEARER_PREFIX:
        return None
    token = text[len(BEARER_PREFIX) :].strip()
    return token or None


async def require_api_key(request: Request, deps: ServiceDeps) -> None:
    """校验 Bearer 密钥；未配置密钥时直接放行。

    ``AGENT_SERVICE_API_KEY`` 为空表示本机开发模式，不做认证——默认配置即可
    启动是既有约定，不应因为加了安全模块就要求每个开发者先配密钥。

    Raises:
        ServiceError: 缺少令牌或令牌不匹配，均以 401 ``unauthorized`` 返回。
    """
    settings = deps.settings
    if not settings.auth_enabled:
        return

    presented = _bearer_token(request.headers.get(AUTHORIZATION_HEADER))
    if presented is None:
        raise ServiceError(
            ErrorCode.UNAUTHORIZED,
            "missing bearer token",
            detail=f"send an '{AUTHORIZATION_HEADER.capitalize()}: Bearer <key>' header",
        )
    # 定长比较，避免用比较耗时反推密钥内容。
    if not secrets.compare_digest(presented, settings.api_key):
        raise ServiceError(
            ErrorCode.UNAUTHORIZED,
            "invalid bearer token",
            detail="the provided key does not match AGENT_SERVICE_API_KEY",
        )


# ---------------------------------------------------------------------------
# 令牌桶限流
# ---------------------------------------------------------------------------


@dataclass
class _Bucket:
    """单个客户端的令牌状态。"""

    tokens: float
    updated_at: float


class TokenBucketRateLimiter:
    """按客户端标识计数的令牌桶。

    桶容量即每分钟配额，按 :data:`RATE_LIMIT_WINDOW_SECONDS` 匀速补充：桶空
    之后不用等到整分钟边界，等够一个令牌的时间即可再发一次，比固定窗口更
    平滑。

    单事件循环内 :meth:`check` 是纯同步计算，没有 await 点，因此不存在并发
    交错，不需要加锁。

    Args:
        capacity: 桶容量，即每分钟允许的请求数。
        window_seconds: 配额窗口秒数。
        clock: 单调时钟；测试可注入假时钟以摆脱真实时间。
    """

    MAX_TRACKED_CLIENTS = 4096
    """同时跟踪的客户端上限，超过后丢弃已回满的桶以限制内存。"""

    def __init__(
        self,
        *,
        capacity: int,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.capacity = float(capacity)
        self._refill_per_second = self.capacity / window_seconds
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _Bucket] = {}

    def check(self, key: str) -> float:
        """尝试消费一个令牌。

        Args:
            key: 客户端标识。

        Returns:
            ``0.0`` 表示放行；大于 0 表示被限流，数值为建议等待秒数。
        """
        now = self._clock()
        self._forget_idle()

        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self.capacity, updated_at=now)
            self._buckets[key] = bucket
        else:
            elapsed = max(now - bucket.updated_at, 0.0)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self._refill_per_second)
            bucket.updated_at = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        missing = 1.0 - bucket.tokens
        return missing / self._refill_per_second

    def tracked_clients(self) -> int:
        """当前跟踪的客户端数量，供测试与诊断使用。"""
        return len(self._buckets)

    def _forget_idle(self) -> None:
        """桶已回满等价于「从未被限流」，条目过量时丢弃这些以限制内存。"""
        if len(self._buckets) <= self.MAX_TRACKED_CLIENTS:
            return
        for key in [k for k, b in self._buckets.items() if b.tokens >= self.capacity]:
            del self._buckets[key]


def client_key(request: Request) -> str:
    """客户端标识：优先 ``X-Client-Id``，否则用来源地址。

    同一个来源地址上的多个调用方可以各自报标识，避免互相挤占配额。
    """
    declared = (request.headers.get(CLIENT_ID_HEADER) or "").strip()
    if declared:
        return f"id:{declared}"
    host = request.client.host if request.client is not None else "unknown"
    return f"ip:{host}"


def rate_limiter_for(deps: AgentServiceDeps) -> TokenBucketRateLimiter:
    """取容器上的限流器，缺失时按当前配置补建。

    启动阶段已由 :mod:`agent_service.lifespan` 建好；测试注入的容器可能没带，
    这里补建一次，避免限流在测试环境下静默失效。
    """
    limiter = deps.rate_limiter
    if limiter is None:
        limiter = TokenBucketRateLimiter(capacity=deps.settings.rate_limit_per_minute)
        deps.rate_limiter = limiter
    return limiter


async def enforce_rate_limit(request: Request, deps: ServiceDeps) -> None:
    """按客户端标识扣令牌，超配额抛 429。

    Raises:
        ServiceError: 配额耗尽；``Retry-After`` 头给出建议退避秒数。
    """
    limiter = rate_limiter_for(deps)
    retry_after = limiter.check(client_key(request))
    if retry_after <= 0.0:
        return
    raise ServiceError(
        ErrorCode.RATE_LIMITED,
        "rate limit exceeded",
        detail=f"limit={deps.settings.rate_limit_per_minute} requests per minute",
        headers={RETRY_AFTER_HEADER: str(max(math.ceil(retry_after), 1))},
    )


# ---------------------------------------------------------------------------
# 请求体大小限制
# ---------------------------------------------------------------------------


class _BodyTooLargeError(Exception):
    """内部信号：请求体累计字节数超过上限。

    不继承 :class:`ServiceError`——它不是业务语义的错误，而是「读到一半必须
    中断」的控制流，因此用独立类型避免被异常处理器当成业务错误二次包装。
    """


def _header_value(scope: dict[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == name:
            return value.decode("latin-1")
    return None


def _declared_length(scope: dict[str, Any]) -> int | None:
    """读取 ``content-length``；缺失或非法均返回 ``None``。"""
    raw = _header_value(scope, b"content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _max_body_bytes(scope: dict[str, Any], default: int) -> int:
    """从应用状态取请求体上限，取不到时用缺省值。"""
    deps = getattr(getattr(scope.get("app"), "state", None), "deps", None)
    limit = getattr(getattr(deps, "settings", None), "max_body_bytes", None)
    if isinstance(limit, int) and limit > 0:
        return limit
    return default


def _request_id(scope: dict[str, Any]) -> str | None:
    state = scope.get("state")
    if isinstance(state, dict):
        return state.get("request_id")
    return None


async def _reject(
    scope: dict[str, Any], receive: Any, send: Any, *, limit: int, received: int
) -> None:
    """返回统一的 413 信封。

    ``receive`` 只为满足 ASGI 调用约定而透传：响应不带请求体，不会读它。
    """
    response = error_response(
        413,
        ErrorCode.PAYLOAD_TOO_LARGE.value,
        "request body is too large",
        detail=f"limit={limit} bytes received={received} bytes",
        request_id=_request_id(scope),
    )
    await response(scope, receive, send)


class BodySizeLimitMiddleware:
    """请求体大小限制（纯 ASGI，不缓存响应体，与 SSE 兼容）。

    两道关卡：

    1. ``content-length`` 声明超限时立即拒绝，请求体一个字节都不读——这是
       常规 JSON 客户端的路径，成本最低；
    2. 未声明长度（分块传输）时包装 ``receive`` 累计字节数，超限即抛内部信号
       中断读取并返回 413。

    第 2 条只在响应尚未开始时才能改写状态码；响应已发出则交由上层断开连接，
    因为 HTTP 状态行无法回滚。

    Args:
        app: 下游 ASGI 应用。
        default_max_bytes: 取不到配置时的上限。
    """

    def __init__(
        self,
        app: Callable[..., Any],
        *,
        default_max_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self.app = app
        self.default_max_bytes = default_max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        limit = _max_body_bytes(scope, self.default_max_bytes)
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await _reject(scope, receive, send, limit=limit, received=declared)
            return

        received = 0
        response_started = False

        async def receive_wrapper() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    raise _BodyTooLargeError
            return message

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except _BodyTooLargeError:
            if response_started:
                raise
            await _reject(scope, receive, send, limit=limit, received=received)


__all__ = [
    "AUTHORIZATION_HEADER",
    "BEARER_PREFIX",
    "CLIENT_ID_HEADER",
    "DEFAULT_MAX_BODY_BYTES",
    "RATE_LIMIT_WINDOW_SECONDS",
    "RETRY_AFTER_HEADER",
    "BodySizeLimitMiddleware",
    "TokenBucketRateLimiter",
    "client_key",
    "enforce_rate_limit",
    "rate_limiter_for",
    "require_api_key",
]
