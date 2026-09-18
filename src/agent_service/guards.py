"""请求级闸门：并发上限、排队超时、总时限、断开检测与幂等回放。

职责边界：只负责「这一次请求允许占用多少资源、允许跑多久、要不要直接回放
缓存」，不掺业务逻辑。所有拒绝都转成 :class:`ServiceError`，由统一异常处理器
映射为 429 / 504 与稳定错误码。

用法::

    async with request_slot(deps):
        answer = await generator.answer(question)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .deps import AgentServiceDeps
from .errors import ErrorCode, ServiceError

logger = logging.getLogger(__name__)

DisconnectProbe = Callable[[], Awaitable[bool]]
"""无参协程，返回客户端是否已断开。路由传 ``request.is_disconnected``。"""

IDEMPOTENCY_HEADER = "Idempotency-Key"
"""客户端提供幂等键的请求头。"""

REPLAY_HEADER = "Idempotency-Replayed"
"""命中缓存时附加的响应头，取值为 ``true``。"""


@asynccontextmanager
async def request_slot(deps: AgentServiceDeps) -> AsyncIterator[None]:
    """占用一个并发槽位，并给本次请求设定总时限。

    Raises:
        ServiceError: 排队等待超过 ``queue_timeout_seconds``（限流），或整体
            处理时间超过 ``request_timeout_seconds``（超时）。
    """
    settings = deps.settings
    gate = deps.gate
    try:
        await asyncio.wait_for(gate.acquire(), timeout=settings.queue_timeout_seconds)
    except TimeoutError as exc:
        raise ServiceError(
            ErrorCode.RATE_LIMITED,
            "service concurrency limit reached, retry later",
            detail=(
                f"queue_timeout={settings.queue_timeout_seconds}s "
                f"max_concurrency={settings.max_concurrency}"
            ),
        ) from exc

    try:
        async with asyncio.timeout(settings.request_timeout_seconds):
            yield
    except TimeoutError as exc:
        raise ServiceError(
            ErrorCode.TIMEOUT,
            "request timed out",
            detail=f"request_timeout={settings.request_timeout_seconds}s",
        ) from exc
    finally:
        gate.release()


def make_disconnect_probe(candidate: object) -> DisconnectProbe:
    """把 Starlette 的 ``request.is_disconnected`` 包成无参协程。

    直接把它当无参函数传下去会丢掉绑定关系，这里显式闭包一次，使路由与测试
    拿到完全相同的调用形态。
    """
    method = getattr(candidate, "is_disconnected", None)
    if method is None:

        async def _never() -> bool:
            return False

        return _never

    async def _probe() -> bool:
        return bool(await method())

    return _probe


async def guard_stream(probe: DisconnectProbe, *, path: str) -> bool:
    """流式响应每帧调用的检查。

    返回 ``True`` 表示应当停止推送：客户端已经断开，继续生成只会白烧算力。
    判定集中在这一处，日志也只记一条，避免每帧都刷错误。
    """
    try:
        disconnected = await probe()
    except Exception as exc:  # noqa: BLE001 —— 探测本身失败不应中断响应
        logger.debug("disconnect probe failed: %s", exc)
        return False
    if disconnected:
        logger.info("stream cancelled by client path=%s code=%s", path, ErrorCode.CANCELLED.value)
        return True
    return False


def idempotent_replay(deps: AgentServiceDeps, request: Request, body: Any) -> JSONResponse | None:
    """命中幂等缓存时返回可回放的响应，否则返回 ``None``。

    没有请求头或存储未就绪时直接返回 ``None``：幂等是可选增强，缺了它请求
    照常执行，不该因此报错。
    """
    store = deps.sessions
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if store is None or not key:
        return None
    entry = store.lookup(key, body)
    if entry is None:
        return None
    # 命中数进指标：它是「客户端重试率」最直接的观测，也是幂等是否生效的证据。
    deps.metrics.record_idempotency_replay()
    return JSONResponse(
        status_code=entry.status_code,
        content=entry.payload,
        headers={REPLAY_HEADER: "true"},
    )


def idempotent_remember(
    deps: AgentServiceDeps,
    request: Request,
    body: Any,
    *,
    status_code: int,
    payload: dict[str, Any],
) -> None:
    """把一次成功执行的响应写入幂等缓存。"""
    store = deps.sessions
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if store is None or not key:
        return
    store.store(key, body, status_code=status_code, payload=payload)
