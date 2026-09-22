"""追踪上下文：当前 trace、当前 span 与请求标识。

用 :class:`contextvars.ContextVar` 而不是实例属性来保存「当前 span」，原因是
**并发隔离**：每个 ``asyncio.Task`` 创建时会拷贝一份当前上下文，因此两个并发
请求各自维护自己的 span 栈，父节点不会互相串。若改用追踪器实例上的列表当栈，
两个并发请求会把彼此的 span 认成父子，产出无法解读的 trace 树。

请求标识不另建机制，直接复用 :data:`logging_config.request_id_var`：请求中间件
已经在入口 set、在 ``finally`` 里 reset，追踪层只需读。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

from logging_config import request_id_var

current_trace_id_var: ContextVar[str | None] = ContextVar("obs_trace_id", default=None)
"""当前 trace 标识；不在任何 trace 上下文里时为 ``None``。"""

last_trace_id_var: ContextVar[str | None] = ContextVar("obs_last_trace_id", default=None)
"""本请求最近一次开启过的 trace 标识，**根 span 收尾后仍然保留**。

与 :data:`current_trace_id_var` 的区别：后者在根 span 结束时被清空，
因为「当前」的语义要求它如此；但异常处理器是在根 span 收尾**之后**才运行的
（异常穿出 ``with tracer.trace()`` 时上下文已被复位），此时若只能读
``current_trace_id_var`` 就拿不到标识，错误响应便无法带上 ``X-Trace-Id``。
因此另存一份只增不清的副本，专供错误路径取值。
"""

current_span_id_var: ContextVar[str | None] = ContextVar("obs_span_id", default=None)
"""当前 span 标识，即下一个新建 span 的父节点。"""


def get_trace_id() -> str | None:
    """当前 trace 标识。"""
    return current_trace_id_var.get()


def get_last_trace_id() -> str | None:
    """本请求最近一次开启过的 trace 标识，根 span 收尾后仍可读到。"""
    return last_trace_id_var.get()


def set_trace_id(trace_id: str | None) -> Token[str | None]:
    """写入当前 trace 标识，返回可用于复位的 token。

    标识非空时同时记入 :data:`last_trace_id_var`；传 ``None`` 只清「当前」，
    不清「最近」——后者正是给根 span 收尾后的错误路径留着看的。
    """
    token = current_trace_id_var.set(trace_id)
    if trace_id is not None:
        last_trace_id_var.set(trace_id)
    return token


def get_span_id() -> str | None:
    """当前 span 标识。"""
    return current_span_id_var.get()


def set_span_id(span_id: str | None) -> Token[str | None]:
    """写入当前 span 标识，返回可用于复位的 token。"""
    return current_span_id_var.set(span_id)


def reset(token: Token[Any]) -> None:
    """按 token 复位上下文。

    必须与 :func:`set_trace_id` / :func:`set_span_id` 成对使用，且放在
    ``finally`` 里：span 退出时若不复位，后续同任务内新建的 span 会错误地把
    已结束的 span 当作父节点。
    """
    token.var.reset(token)


def current_request_id() -> str | None:
    """当前请求标识，取自请求中间件注入的上下文变量。"""
    return request_id_var.get()


__all__ = [
    "current_request_id",
    "current_span_id_var",
    "current_trace_id_var",
    "get_last_trace_id",
    "get_span_id",
    "get_trace_id",
    "last_trace_id_var",
    "reset",
    "set_span_id",
    "set_trace_id",
]
