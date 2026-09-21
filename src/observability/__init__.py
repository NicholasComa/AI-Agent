"""可观测性追踪层：给模型调用、检索、工具与工作流节点留下可检索的执行记录。

分层
----

============  ==========================================================
模块          职责
============  ==========================================================
``config``    读 ``OBS_*`` 与 ``LANGFUSE_*`` 环境变量，解析失败静默回退
``models``    记录数据结构与纯函数（哈希、清洗、用量组装）
``context``   当前 trace / 当前 span 的上下文变量，供嵌套推导父节点
``pricing``   token 用量到成本的折算表
``tracer``    门面：开 trace、开 span、标记错误、收尾与释放
``backends``  后端实现与降级链：远端 → 本地 JSONL → 内存
============  ==========================================================

设计约束
--------

1. 导入本包**不产生副作用**：不连网、不起线程、不读文件，远端 SDK 也只在
   真正选中该后端时才导入。
2. 默认**不记录正文**。提示词与回答只以哈希形式出现，原文需要显式打开内容
   捕获开关才会记录，且该开关默认关闭。
3. 追踪是旁路能力：任何一层失败都只降级并记日志，不向业务链路抛异常。
"""

from __future__ import annotations

from .backends import (
    BackendUnavailableError,
    InMemoryBackend,
    JsonlBackend,
    TraceBackend,
    build_backend,
    langfuse_sdk_available,
)
from .config import (
    BACKENDS,
    ENV_PREFIX,
    LANGFUSE_ENV_PREFIX,
    ObservabilityConfig,
    describe,
)
from .models import (
    SPAN_KINDS,
    SpanRecord,
    content_payload,
    make_usage,
    new_trace_id,
    sanitize_attributes,
)
from .tracer import Tracer, build_tracer

__all__ = [
    "BACKENDS",
    "ENV_PREFIX",
    "LANGFUSE_ENV_PREFIX",
    "SPAN_KINDS",
    "BackendUnavailableError",
    "InMemoryBackend",
    "JsonlBackend",
    "ObservabilityConfig",
    "SpanRecord",
    "TraceBackend",
    "Tracer",
    "build_backend",
    "build_tracer",
    "content_payload",
    "describe",
    "langfuse_sdk_available",
    "make_usage",
    "new_trace_id",
    "sanitize_attributes",
]
