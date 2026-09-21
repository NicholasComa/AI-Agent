"""追踪后端工厂与降级链。

降级顺序：``langfuse`` → ``jsonl`` → ``memory``。任何一层不可用都只把原因
写进 :attr:`TraceBackend.degraded_reason`，**绝不抛异常、绝不阻断服务启动**。
追踪是旁路能力，它坏掉时的正确行为是「安静地少记一点」，而不是让业务请求
返回 500。

为什么降级判定要显式检查而不靠异常兜底
--------------------------------------

远端 SDK 官方只承诺「SDK 内部的错误会被捕获并记录」，并没有承诺「缺少凭据
时抛出异常」。如果写成 ``try: get_client() except Exception: fallback``，缺
凭据时很可能既不抛也不上报，于是静默变成「以为在追踪、其实什么都没发」——
这类故障没有任何报错可查，排查成本极高。因此这里的判定是显式的：
先看 SDK 能不能导入，再看两个凭据是不是都非空，两条都过了才尝试真正构造。
"""

from __future__ import annotations

import importlib.util
import logging

from ..config import ObservabilityConfig
from .base import BackendUnavailableError, InMemoryBackend, TraceBackend
from .jsonl import JsonlBackend

logger = logging.getLogger(__name__)

LANGFUSE_MODULE = "langfuse"
"""远端 SDK 的模块名。"""


def langfuse_sdk_available() -> bool:
    """探测远端 SDK 是否可导入。

    用 :func:`importlib.util.find_spec` 探测而**不真的 import**：SDK 在导入期
    会注册退出钩子并启动后台线程，服务启动阶段不需要为一次探测付出这个代价。
    探测本身也可能因依赖缺失而报错，此时按「不可用」处理。
    """
    try:
        return importlib.util.find_spec(LANGFUSE_MODULE) is not None
    except (ImportError, ValueError):
        return False


def _brief(exc: BaseException, *, limit: int = 160) -> str:
    """把异常压成一行短摘要，用于降级原因展示。"""
    text = " ".join(str(exc).split())
    return text[:limit] if text else type(exc).__name__


def _try_jsonl(config: ObservabilityConfig, *, reason: str | None = None) -> TraceBackend:
    """构造本地后端；目录不可用时退到内存后端。"""
    try:
        return JsonlBackend(config.trace_dir, reason=reason)
    except (BackendUnavailableError, OSError) as exc:
        fallback_reason = f"jsonl unavailable: {_brief(exc)}"
        if reason:
            fallback_reason = f"{reason}; {fallback_reason}"
        logger.warning("observability.backend_fallback backend=memory reason=%s", fallback_reason)
        return InMemoryBackend(reason=fallback_reason)


def build_backend(config: ObservabilityConfig, *, client: object | None = None) -> TraceBackend:
    """按配置构造追踪后端。

    Args:
        config: 追踪层配置。
        client: 远端 SDK 客户端替身；仅测试使用，为 ``None`` 时由后端自行取
            单例客户端。

    Returns:
        可用的后端实例。即便全部后端都不可用，也会返回一个内存后端而不是抛
        异常，调用方无需处理失败分支。
    """
    if not config.enabled:
        return InMemoryBackend(reason="disabled (OBS_ENABLED=false)")

    if config.backend != "langfuse":
        return _try_jsonl(config)

    reasons: list[str] = []
    if not langfuse_sdk_available():
        reasons.append("langfuse sdk not installed")
    if not config.langfuse_configured:
        reasons.append("langfuse credentials missing")
    if reasons:
        joined = "; ".join(reasons)
        logger.warning("observability.backend_fallback backend=jsonl reason=%s", joined)
        return _try_jsonl(config, reason=joined)

    from .langfuse import LangfuseBackend  # 延迟导入：不在探测阶段拉起 SDK

    try:
        return LangfuseBackend(config, client=client)
    except BackendUnavailableError as exc:
        reason = f"langfuse init failed: {_brief(exc)}"
        logger.warning("observability.backend_fallback backend=jsonl reason=%s", reason)
        return _try_jsonl(config, reason=reason)


__all__ = [
    "LANGFUSE_MODULE",
    "BackendUnavailableError",
    "InMemoryBackend",
    "JsonlBackend",
    "TraceBackend",
    "build_backend",
    "langfuse_sdk_available",
]
