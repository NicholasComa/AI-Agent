"""服务线协议模型。

只放与 HTTP 边界直接相关的模型。错误信封复用 :mod:`api_models`，
领域对象的模型仍由各自模块提供（``rag.generator.RagAnswer``、
``graph.state.WorkflowState`` 等），服务层不重复定义。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rag.generator import DEFAULT_MIN_SCORE

SERVICE_NAME = "agent-service"
"""``/health`` 与 ``/ready`` 回报的服务名。"""

Backend = Literal["real", "fake"]
"""后端模式：``real`` 表示已接上真实模型接口，``fake`` 表示降级到确定性实现。"""

WorkflowStatus = Literal["completed", "awaiting_clarification"]
"""工作流一次执行的结果状态。"""

MAX_QUESTION_LENGTH = 4000
"""提问长度上限，与离线 RAG 链路的约定保持一致。"""

MAX_REQUIREMENT_LENGTH = 8000
"""需求文本长度上限。"""

MAX_ANSWERS = 10
"""一次补充澄清答案的条数上限。"""

MAX_SESSION_ID = 64
"""会话 ID 长度上限，与 :mod:`agent_service.session` 的校验保持一致。"""


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


class ServiceMetricsSummaryResponse(BaseModel):
    """``GET /metrics-summary`` 响应体：进程内累计指标快照。

    计数器随进程生命周期累加，重启即归零——这是刻意的：本接口服务于「当前这
    个实例现在健康吗」，历史趋势应由第 10 周的 Trace 与评测链路承担。

    Attributes:
        service: 服务名，便于多实例聚合时区分来源。
        uptime_seconds: 已运行秒数，用于判断计数是否覆盖了完整观察窗口。
        requests_total: ``路由|状态码`` 到次数的映射，保留最细粒度。
        requests_by_route: 按路由聚合的请求数。
        requests_by_status: 按状态码聚合的请求数。
        in_flight: 采样瞬间仍在处理中的请求数。
        latency_ms: 端到端延迟统计，含 ``avg`` / ``p95`` / ``samples``。
        llm_calls: 累计模型调用次数（含重试，按实际调用次数计）。
        rag_queries: 累计知识库检索次数。
        rag_hit_rate: 检索命中率，0.0-1.0；无检索时为 0.0。
        errors_by_code: 统一错误码到次数的映射。
        idempotency_replays: 幂等缓存命中次数。
        request_id: 与响应头 ``X-Request-ID`` 同源。
    """

    model_config = ConfigDict(extra="forbid")

    service: str
    uptime_seconds: float = Field(..., ge=0)
    requests_total: dict[str, int] = Field(default_factory=dict)
    requests_by_route: dict[str, int] = Field(default_factory=dict)
    requests_by_status: dict[str, int] = Field(default_factory=dict)
    in_flight: int = Field(..., ge=0)
    latency_ms: dict[str, float] = Field(default_factory=dict)
    llm_calls: int = Field(..., ge=0)
    rag_queries: int = Field(..., ge=0)
    rag_hit_rate: float = Field(..., ge=0.0, le=1.0)
    errors_by_code: dict[str, int] = Field(default_factory=dict)
    idempotency_replays: int = Field(..., ge=0)
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


# ---------------------------------------------------------------------------
# RAG 问答
# ---------------------------------------------------------------------------


class RagQueryRequest(BaseModel):
    """``POST /v1/rag/answer`` 请求体。

    Attributes:
        question: 用户问题。
        top_k: 召回片段数。
        min_score: Top1 相似度下限，低于该值直接拒答，不调用模型。
        stream: 为 ``true`` 时响应改为 ``text/event-stream``，逐帧推送。
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        ...,
        min_length=1,
        max_length=MAX_QUESTION_LENGTH,
        description="用户问题，非空",
    )
    top_k: int = Field(default=3, ge=1, le=20, description="召回片段数")
    min_score: float = Field(
        default=float(DEFAULT_MIN_SCORE),
        ge=0.0,
        le=1.0,
        description="Top1 相似度拒答阈值",
    )
    stream: bool = Field(default=False, description="true 时以 SSE 逐帧推送")


# ---------------------------------------------------------------------------
# 需求分析工作流
# ---------------------------------------------------------------------------


class WorkflowStartRequest(BaseModel):
    """``POST /v1/workflow/requirement-analysis`` 请求体。

    Attributes:
        requirement_text: 原始需求文本。
        session_id: 可选会话标识。传入后线程绑定被持久化，便于续跑与
            进程重启后恢复。
        thread_id: 可选线程标识。由调用方显式指定时以其为准，用于客户端
            自行管理连续性。
        stream: 为 ``true`` 时响应改为 ``text/event-stream``，按节点推帧。
    """

    model_config = ConfigDict(extra="forbid")

    requirement_text: str = Field(
        ...,
        min_length=1,
        max_length=MAX_REQUIREMENT_LENGTH,
        description="原始需求文本",
    )
    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_SESSION_ID,
        description="可选会话标识",
    )
    thread_id: str | None = Field(default=None, description="可选线程标识")
    stream: bool = Field(default=False, description="true 时以 SSE 按节点推送")


class WorkflowResumeRequest(BaseModel):
    """``POST /v1/workflow/{thread_id}/resume`` 请求体。

    Attributes:
        answers: 人工补充的澄清答案，按问题顺序给出。
        stream: 为 ``true`` 时响应改为 ``text/event-stream``。
    """

    model_config = ConfigDict(extra="forbid")

    answers: list[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_ANSWERS,
        description="补充的澄清答案",
    )
    stream: bool = Field(default=False, description="true 时以 SSE 按节点推送")


class WorkflowRunResponse(BaseModel):
    """工作流一次执行的结果。

    Attributes:
        thread_id: 本次执行的线程标识。
        session_id: 关联的会话标识；调用方未提供时为 ``None``。
        status: ``completed`` 表示跑完；``awaiting_clarification`` 表示因
            关键信息不足而挂起，等待补充后调 resume。
        clarification_questions: 挂起时列出待澄清问题。
        report: 报告正文；挂起时为 ``None``。
        trace: 节点执行顺序。
        rag_degraded: 本次是否未检索到内部资料。
        errors: 节点失败记录。
        request_id: 与响应头 ``X-Request-ID`` 同源。
    """

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    session_id: str | None = None
    status: WorkflowStatus
    clarification_questions: list[str] = Field(default_factory=list)
    report: str | None = None
    trace: list[str] = Field(default_factory=list)
    rag_degraded: bool = False
    errors: list[dict[str, Any]] = Field(default_factory=list)
    request_id: str | None = None


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------


class ToolInfo(BaseModel):
    """单个 MCP 工具的元信息。

    Attributes:
        name: 工具名。
        description: 工具说明。
        input_schema: 入参 JSON Schema，供调用方构造参数。
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolListResponse(BaseModel):
    """``GET /v1/tools`` 响应体。

    Attributes:
        tools: 已发现工具列表。
        request_id: 与响应头 ``X-Request-ID`` 同源。
    """

    tools: list[ToolInfo]
    request_id: str | None = None


class ToolCallRequest(BaseModel):
    """``POST /v1/tools/{name}/call`` 请求体。

    Attributes:
        arguments: 传给工具的参数对象，参数校验由工具自身完成。
    """

    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict, description="工具入参")


class ToolCallResponse(BaseModel):
    """工具调用结果。

    ``is_error`` 为 ``true`` 表示工具侧返回了错误结论（参数非法、路径越界
    等），这属于正常业务结果，HTTP 状态码仍为 200；只有 MCP 会话本身不可用
    才返回 503。

    Attributes:
        name: 被调用的工具名。
        is_error: 工具是否返回错误结论。
        structured: 工具的结构化输出信封。
        text: 工具返回的文本内容。
        request_id: 与响应头 ``X-Request-ID`` 同源。
    """

    name: str
    is_error: bool
    structured: dict[str, Any] = Field(default_factory=dict)
    text: str | None = None
    request_id: str | None = None
