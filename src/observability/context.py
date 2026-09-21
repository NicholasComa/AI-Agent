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

current_span_id_var: ContextVar[str | None] = ContextVar("obs_span_id", default=None)
"""当前 span 标识，即下一个新建 span 的父节点。"""


def get_trace_id() -> str | None:
    """当前 trace 标识。"""
    return current_trace_id_var.get()


def set_trace_id(trace_id: str | None) -> Token[str | None]:
    """写入当前 trace 标识，返回可用于复位的 token。"""
    return current_trace_id_var.set(trace_id)


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
    "get_span_id",
    "get_trace_id",
    "reset",
    "set_span_id",
    "set_trace_id",
]
