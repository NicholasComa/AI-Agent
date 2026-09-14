"""服务线协议模型。

只放与 HTTP 边界直接相关的模型。错误信封复用 :mod:`api_models`，
领域对象的模型仍由各自模块提供（``rag.generator.RagAnswer``、
``graph.state.WorkflowState`` 等），服务层不重复定义。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SERVICE_NAME = "agent-service"
"""``/health`` 与 ``/ready`` 回报的服务名。"""

Backend = Literal["real", "fake"]
"""后端模式：``real`` 表示已接上真实模型接口，``fake`` 表示降级到确定性实现。"""


class DependencyState(BaseModel):
    """单个依赖的探活结果。

    Attributes:
        name: 依赖名（``session_store`` / ``config`` / ``qdrant`` / ``llm`` /
            ``workflow`` / ``mcp``）。
        ready: 当前是否可用。
        required: 是否为必需依赖。可选依赖不可用不会让整体判定为未就绪。
        detail: 一行说明，未就绪时给出原因，就绪时给出关键标识。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="依赖名")
    ready: bool = Field(..., description="是否可用")
    required: bool = Field(..., description="是否为必需依赖")
    detail: str = Field(..., description="状态说明")


class ServiceHealthResponse(BaseModel):
    """``GET /health`` 响应体：只回答「进程还活着吗」。

    Attributes:
        status: 必需依赖全部就绪时为 ``ok``，否则为 ``degraded``。
        service: 服务名。
        version: 应用版本。
        backend: 后端模式。
        started_at: 启动时间（ISO 8601）。
        uptime_seconds: 已运行秒数。
        request_id: 与响应头 ``X-Request-ID`` 同源。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    service: str
    version: str
    backend: Backend
    started_at: str
    uptime_seconds: float = Field(..., ge=0)
    request_id: str | None = None


class ServiceReadyResponse(BaseModel):
    """``GET /ready`` 响应体：逐依赖给出可读结论。

    必需依赖未就绪时 HTTP 状态码为 503，响应体仍然完整，调用方可以从
    ``dependencies`` 直接看出是哪一层没起来，无需翻日志。

    Attributes:
        status: 必需依赖全部就绪时为 ``ready``，否则为 ``degraded``。
        dependencies: 全部依赖的探活明细。
        request_id: 与响应头 ``X-Request-ID`` 同源。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "degraded"]
    dependencies: list[DependencyState]
    request_id: str | None = None
