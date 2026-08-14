"""Day 5-9 - HTTP 接口的请求 / 响应 / 错误 Pydantic 模型。

为什么单独拆一个模块（而不是复用 :mod:`src.schemas`）？

* :mod:`src.schemas` 定义的是 **LLM 输出契约** -
  :class:`RequirementAnalysis` 是我们要求模型去填充的 schema。
* 本模块定义的是 **HTTP 线协议契约** - 即 REST 客户端发送什么、
  我们承诺返回什么。部分结构有重叠（例如
  :class:`AnalyzeRequirementResponse` 复用了
  :class:`src.schemas.RequirementAnalysis`），但请求体与错误信封属于
  HTTP 层关注点，放进 LLM 模块会造成污染。

约定：

* 每个请求字段都由 Pydantic 校验。``Field(...)`` 配合
  ``min_length`` / ``max_length`` / ``ge`` / ``le`` 可以把非法输入
  转换成清晰的 ``422 Unprocessable Entity``，并带结构化错误体。
* 每个响应模型都显式设置 ``model_config =
  ConfigDict(extra='forbid')``，这样如果将来某次重构不小心加了字段
  却没更新测试，会立刻报错而非静默通过。
* 错误信封统一使用 :class:`ErrorResponse` -> :class:`ErrorBody`，
  客户端就能统一解析 ``response.json()["error"]["code"]``。
* 响应里可选地携带 ``request_id`` —— 与响应头 ``X-Request-ID`` 同源，
  客户端可以任选其一做关联（Day 8 起）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from schemas import RequirementAnalysis

# ---------------------------------------------------------------------------
# 对话接口：POST /chat
# ---------------------------------------------------------------------------

ChatRole = Literal["system", "user", "assistant"]
"""对话补全接口接受的消息角色。

这里刻意**不允许** ``"tool"`` / ``"function"`` / ``"developer"`` -
第 1 周的客户端只做最普通的对话轮次。以后要支持，只需放宽这个字面量即可。
"""


class ChatMessage(BaseModel):
    """对话中的一条消息。

    Attributes:
        role: 由谁产生这条消息（``system`` / ``user`` / ``assistant``）。
        content: 消息正文。必须非空，这样漏写 ``content`` 字段时会变成
            422 而不是一条静默的空消息。
    """

    model_config = ConfigDict(extra="forbid")

    role: ChatRole = Field(..., description="消息角色：system / user / assistant")
    content: str = Field(..., min_length=1, description="消息正文，非空")


class ChatRequest(BaseModel):
    """``POST /chat`` 的请求体。

    Attributes:
        messages: 有序的对话轮次（至少一条）。
        model: 可选的模型覆盖；缺省时回退到配置的默认值。
        temperature: 可选的采样温度（``0.0`` = 确定性输出，
            ``1.0`` = 默认值，``> 1.5`` 通常没意义）。仅当显式设置时才转发给 LLM。
        max_tokens: 可选的生成 token 数上限。
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(..., min_length=1, description="对话消息列表，至少 1 条")
    model: str | None = Field(default=None, description="可选模型覆盖；缺省用配置默认值")
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="采样温度 0.0-2.0；不传则不发送 temperature 字段",
    )
    max_tokens: int | None = Field(
        default=None,
        ge=1,
        le=8192,
        description="可选的最大生成 token 数",
    )


class ChatResponse(BaseModel):
    """``POST /chat`` 返回的响应体。

    Attributes:
        text: 助手的回复文本。
        model: 服务端回显的实际模型名（当网关发生回退时可能与请求中不同）。
        elapsed_ms: 底层调用的墙上时钟耗时。
        usage: 服务端返回的可选 token 用量字典。
        request_id: 服务端 request_id（与响应头 ``X-Request-ID`` 同源）。
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., description="助手回复正文")
    model: str = Field(..., description="实际使用的模型名（可能与请求不同）")
    elapsed_ms: int = Field(..., ge=0, description="底层调用耗时，毫秒")
    usage: dict[str, int] | None = Field(default=None, description="可选 token 用量")
    request_id: str | None = Field(
        default=None,
        description="服务端 request_id（与响应头 X-Request-ID 同源）",
    )


# ---------------------------------------------------------------------------
# 流式接口：POST /chat/stream  ——  Day 9 落地，契约先在这里定义
# ---------------------------------------------------------------------------


class ChatChunk(BaseModel):
    """``POST /chat/stream`` 的单片响应（SSE ``data: {...}`` 解析后形态）。

    Attributes:
        delta: 本片增量文本（OpenAI 兼容 ``choices[0].delta.content``）。
        done: 是否为终止哨兵（对应 SSE ``data: [DONE]``）。
    """

    model_config = ConfigDict(extra="forbid")

    delta: str = Field(..., description="增量文本")
    done: bool = Field(default=False, description="是否为终止哨兵")


# ---------------------------------------------------------------------------
# 需求分析接口：POST /analyze-requirement
# ---------------------------------------------------------------------------


class AnalyzeRequirementRequest(BaseModel):
    """``POST /analyze-requirement`` 的请求体。

    Attributes:
        text: 客户的原始输入。必须是 2-4000 字符，这样像 ``""`` / ``"x"``
            这样无意义的提交不会浪费一次 LLM 调用，过长的输入也会在边界被拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        ...,
        min_length=2,
        max_length=4000,
        description="客户原始需求文字，2-4000 字符",
    )


class AnalyzeRequirementResponse(RequirementAnalysis):
    """``POST /analyze-requirement`` 返回的响应体。

    复用 :class:`src.schemas.RequirementAnalysis` 的全部六个字段
    （即 LLM 契约）。保留这个别名意味着测试可以直接用 LLM schema 校验
    响应，而无需重新声明一遍。

    Day 8 起额外携带 ``request_id``，方便客户端关联日志。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(
        default=None,
        description="服务端 request_id（与响应头 X-Request-ID 同源）",
    )


# ---------------------------------------------------------------------------
# Dify Workflow 接口：POST /dify/run（通用透传）
# ---------------------------------------------------------------------------


class DifyRunRequest(BaseModel):
    """``POST /dify/run`` 的请求体。

    Attributes:
        query: 要交给 Dify 工作流 `开始` 节点的用户请求。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="用户原始请求，将原样传入 Dify 工作流的 query 变量",
    )


class DifyRunResponse(BaseModel):
    """``POST /dify/run`` 的响应体。

    ``outputs`` 即 Dify ``data.outputs``，因为不同工作流输出结构不同，
    这里用 ``dict[str, Any]`` 透传，调用方按实际工作流字段自行解析。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., description="原始请求")
    outputs: dict[str, Any] = Field(
        ...,
        description="Dify 工作流输出（字段由所调用工作流决定，例如 text_calc / final_output 等）",
    )
    status: str | None = Field(default=None, description="Dify 执行状态")
    elapsed_time: float | None = Field(default=None, description="Dify 执行耗时（秒）")
    total_tokens: int | None = Field(default=None, description="总 token 数")
    workflow_run_id: str | None = Field(default=None, description="Dify workflow_run_id")
    request_id: str | None = Field(
        default=None,
        description="服务端 request_id（与响应头 X-Request-ID 同源）",
    )


# ---------------------------------------------------------------------------
# 健康检查：GET /health
# ---------------------------------------------------------------------------


class HealthModelInfo(BaseModel):
    """``/health`` 暴露的配置摘要。

    刻意不包含 API key。``key_configured`` 是一个由 ``bool(api_key)``
    派生的布尔值 - 它可以被安全地记录 / 返回。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="默认模型名")
    base_url: str = Field(..., description="模型服务 base URL（不含尾部斜杠）")
    timeout_seconds: float = Field(..., gt=0, description="单次请求超时（秒）")
    enable_stream: bool = Field(..., description="是否启用流式")
    key_configured: bool = Field(..., description="API_KEY 是否已配置（非空）")
    provider: str = Field(..., description="provider 名称（openai_compatible / ollama）")
    max_concurrency: int = Field(..., gt=0, description="上游并发上限")


class HealthResponse(BaseModel):
    """``GET /health`` 返回的响应体。

    Attributes:
        status: 配置齐全时为 ``"ok"``；API key 缺失时为 ``"degraded"``
            （服务器本身仍在线，但 ``/chat`` 和 ``/analyze-requirement``
            会返回 401）。
        version: 包版本号（取自 ``pyproject.toml``）。
        model: 模型配置摘要。
        provider: provider 名称（顶层快查字段，与 ``model.provider`` 同值）。
        max_concurrency: 上游并发上限（顶层快查字段）。
        request_id: 服务端 request_id（与响应头 ``X-Request-ID`` 同源）。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"] = Field(..., description="总体健康状态")
    version: str = Field(..., description="应用版本（来自 pyproject.toml）")
    model: HealthModelInfo = Field(..., description="模型配置摘要")
    provider: str = Field(..., description="provider 名称（顶层快查）")
    max_concurrency: int = Field(..., gt=0, description="上游并发上限（顶层快查）")
    request_id: str | None = Field(
        default=None,
        description="服务端 request_id（与响应头 X-Request-ID 同源）",
    )


# ---------------------------------------------------------------------------
# 模型清单：GET /models   ——   Day 8 新增
# ---------------------------------------------------------------------------


class ModelInfo(BaseModel):
    """单模型元信息（``GET /models`` 列表中的元素）。

    Attributes:
        name: 模型名称。
        base_url: 模型服务 base URL（不含尾部斜杠）。
        provider: provider 名称。
        key_configured: API key 是否已配置（布尔；绝不暴露 key 本身）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="模型名称")
    base_url: str = Field(..., description="模型服务 base URL")
    provider: str = Field(..., description="provider 名称")
    key_configured: bool = Field(..., description="API key 是否已配置")


class ModelsResponse(BaseModel):
    """``GET /models`` 返回的响应体。

    Attributes:
        models: 可用模型列表。第一个是当前默认（与 ``current`` 同名），
            其余来自 :attr:`config.AppConfig.extra_models`。
        current: 当前默认模型名。
        request_id: 服务端 request_id。
    """

    model_config = ConfigDict(extra="forbid")

    models: list[ModelInfo] = Field(..., description="可用模型列表（当前 + 兜底）")
    current: str = Field(..., description="当前默认模型名")
    request_id: str | None = Field(
        default=None,
        description="服务端 request_id（与响应头 X-Request-ID 同源）",
    )


# ---------------------------------------------------------------------------
# 错误信封
# ---------------------------------------------------------------------------


class ErrorBody(BaseModel):
    """单条错误信息。

    Attributes:
        code: 机器可读的标识（例如 ``"llm_timeout"``、
            ``"validation_error"``、``"llm_auth"``）。
        message: 人类可读的一行描述。可以安全地暴露给客户端 -
            绝不包含 API key 或请求体。
        detail: 可选的结构化细节（例如底层异常的类名），用于调试。
        status_code: HTTP 状态码。
        request_id: 服务端 request_id（与响应头 ``X-Request-ID`` 同源），
            流式与非流式错误体都携带，便于客户端关联日志。
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., description="机器可读错误码")
    message: str = Field(..., description="人类可读描述")
    detail: str | None = Field(default=None, description="可选的调试细节")
    status_code: int = Field(..., description="HTTP状态码", ge=100, lt=600)
    request_id: str | None = Field(
        default=None,
        description="服务端 request_id（与响应头 X-Request-ID 同源）",
    )


class ErrorResponse(BaseModel):
    """所有接口在失败时返回的统一错误信封。

    每个非 2xx 响应都形如::

        {"error": {"code": "...", "message": "...", "detail": "..."}}

    FastAPI 自动返回的 422 ``RequestValidationError`` 也会被改写成这种
    形状，这样客户端只需解析一种错误格式。
    """

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody = Field(..., description="错误体")


# ---------------------------------------------------------------------------
# 内部辅助函数（不作为接口导出）
# ---------------------------------------------------------------------------


def error_body(
    code: str, message: str, detail: str, http_status: int | None = None
) -> dict[str, Any]:
    """构造一个 :class:`ErrorResponse` 形状的字典，供各 handler 使用。

    Args:
        code: 机器可读的标识（例如 ``"llm_timeout"``）。
        message: 人类可读的一行描述。
        detail: 可选的结构化细节。
        http_status: HTTP 状态码。若为 ``None``，则不在字典中返回。
    Returns:
        ``{"error": {"code": ..., "message": ..., "detail": ..., "status_code": ...}}``
    """
    return {
        "error": {"code": code, "message": message, "detail": detail, "status_code": http_status}
    }
