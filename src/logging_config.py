"""Day 8 - JSON 结构化日志 + request_id 注入。

提供 4 个公开符号:

* :data:`request_id_var` —— :class:`contextvars.ContextVar`,在
  :class:`RequestIdMiddleware` 里 set,在 :class:`RequestIdFilter` 里 get。
* :class:`RequestIdFilter` —— 给 handler 加的 filter,把 request_id
  挂到 ``record.request_id``,让 formatter 可以引用。
* :class:`JSONFormatter` / :class:`PlainFormatter` —— 两种输出格式。
* :func:`configure_logging` —— 幂等的日志初始化入口。

设计取舍
--------

* **幂等** :func:`configure_logging` 重复调用仅替换我们之前添加的 handler,
  不影响 pytest caplog / uvicorn 的 handler。
* **additive**:不清空 root.handlers。caplog 仍然能捕获(通过 propagation)。
* **不读 body**:Filter 只读 contextvar,formatter 也只读 record 字段。
* **extras 透传**:`logger.info("access", extra={...})` 中的 extras 会被
  JSONFormatter 自动合并到输出 JSON 的顶层键里(例如 ``method`` /
  ``path`` / ``status_code`` / ``duration_ms``)。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

# ---------------------------------------------------------------------------
# request_id contextvar
# ---------------------------------------------------------------------------

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
"""当前请求的 request_id。

:class:`middleware.RequestIdMiddleware` 在请求开始 set,响应结束 reset。
下游所有日志 / 路由 handler 都可以通过
``logging_config.request_id_var.get()`` 拿到(同一 task 内的协程共享上下文)。
"""

# ---------------------------------------------------------------------------
# 类型
# ---------------------------------------------------------------------------

LogFormat = Literal["json", "plain"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# 标准 LogRecord 属性(用于过滤 extras)
_STD_RECORD_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "request_id",
        "taskName",
    }
)

# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class RequestIdFilter(logging.Filter):
    """从 :data:`request_id_var` 读取 request_id,挂到 log record。

    加到 :class:`logging.StreamHandler` 上(不放在 logger 上,因为 logger
    的 filter 不会自动传播到下游 handler,每个 handler 仍需独立 addFilter)。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 若 record 已自带 request_id(例如 AccessLogMiddleware 通过
        # extra 透传),则尊重之,不覆盖。否则从 ContextVar 取(路由 handler
        # 内日志走此分支);取不到则兜底为 "-"。
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get() or "-"
        return True


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """输出单行 JSON,字段::

        {"ts": "ISO8601 ms UTC", "level": ..., "logger": ...,
         "message": ..., "request_id": ..., <extras>}

    ``<extras>`` 是 ``logger.info(msg, extra={...})`` 传入的额外字段
    (例如 ``method`` / ``path`` / ``status_code`` / ``duration_ms``),
    会自动展平到顶层。
    """

    def format(self, record: logging.LogRecord) -> str:
        # ISO8601 毫秒精度 UTC + 'Z' 后缀
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
        ms = int(getattr(record, "msecs", 0))
        ts = f"{ts}.{ms:03d}Z"

        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        # extras 自动合并
        for key, value in record.__dict__.items():
            if key in _STD_RECORD_KEYS or key.startswith("_"):
                continue
            payload[key] = value

        return json.dumps(payload, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    """人类可读的纯文本格式,适合本地 tail -f。

    例::

        10:23:45 INFO    [req_id] middleware: access
    """

    DEFAULT = "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s"

    def __init__(self) -> None:
        super().__init__(self.DEFAULT, datefmt="%H:%M:%S")


# ---------------------------------------------------------------------------
# 配置入口
# ---------------------------------------------------------------------------

_attached_handler: logging.Handler | None = None
_attached_filter: RequestIdFilter | None = None


def configure_logging(
    level: LogLevel | str = "INFO",
    fmt: LogFormat | str = "json",
) -> None:
    """初始化 root logger。

    幂等:重复调用仅替换我们之前挂上去的 handler / filter,不破坏
    pytest caplog / uvicorn 的 handler。

    Args:
        level: ``"DEBUG"`` / ``"INFO"`` / ``"WARNING"`` / ``"ERROR"``。
        fmt: ``"json"`` (默认) 或 ``"plain"``。
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # 移除我们之前挂上去的 handler(若有)
    global _attached_handler, _attached_filter
    if _attached_handler is not None:
        root.removeHandler(_attached_handler)
        if _attached_filter is not None:
            _attached_handler.removeFilter(_attached_filter)
        _attached_handler = None
        _attached_filter = None

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONFormatter() if fmt == "json" else PlainFormatter())
    f = RequestIdFilter()
    handler.addFilter(f)
    root.addHandler(handler)
    _attached_handler = handler
    _attached_filter = f


__all__ = [
    "JSONFormatter",
    "PlainFormatter",
    "RequestIdFilter",
    "configure_logging",
    "request_id_var",
]
