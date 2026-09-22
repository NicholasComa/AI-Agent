"""MCP 工具路由。

把 MCP Server 的能力接到 HTTP 上：``GET /v1/tools`` 列出工具，
``POST /v1/tools/{name}/call`` 转发调用。

错误分层是这里的关键约定：工具自身返回的失败（参数非法、路径越界）是**业务
结果**，用 HTTP 200 + ``is_error=true`` 加结构化信封返回；只有 MCP 会话本身
不可用才返回 503。混在一起会让调用方无法区分「我的参数错了」和「服务挂了」。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from observability import traced_tool

from ..deps import ServiceDeps
from ..errors import ErrorCode, ServiceError, get_request_id
from ..guards import request_slot
from ..schemas import ToolCallRequest, ToolCallResponse, ToolInfo, ToolListResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/tools", tags=["tools"])


def _text_of(result: Any) -> str | None:
    """取出工具返回的首段文本内容，没有则返回 ``None``。"""
    chunks: list[str] = []
    for item in getattr(result, "content", None) or ():
        text = getattr(item, "text", None)
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n".join(chunks) if chunks else None


@router.get("", response_model=ToolListResponse, summary="列出 MCP 工具")
async def list_tools(request: Request, deps: ServiceDeps) -> ToolListResponse:
    """返回当前会话已发现的工具清单。"""
    session = deps.require("mcp")
    async with request_slot(deps):
        listing = await session.list_tools()
    tools = [
        ToolInfo(
            name=tool.name,
            description=tool.description or "",
            input_schema=dict(getattr(tool, "inputSchema", None) or {}),
        )
        for tool in listing.tools
    ]
    return ToolListResponse(tools=tools, request_id=get_request_id(request))


@router.post(
    "/{name}/call",
    response_model=ToolCallResponse,
    summary="调用 MCP 工具",
    description="转发参数给指定工具并返回其结构化信封；工具级错误以 is_error=true 呈现。",
)
async def call_tool(
    name: str,
    request_body: ToolCallRequest,
    request: Request,
    deps: ServiceDeps,
) -> ToolCallResponse:
    """调用一个工具。"""
    session = deps.require("mcp")
    request_id = get_request_id(request)
    # 埋点包在会话调用外面，记录工具名与参数键；参数值不进 span（可能含路径
    # 与提交信息正文）。track 只影响记录，异常仍按下面的分层原样上抛。
    traced = traced_tool(session.call_tool, deps.tracer, tool_name=name)
    try:
        async with request_slot(deps):
            result = await traced(name, request_body.arguments)
    except ServiceError:
        # 闸门或超时产生的错误已经带好错误码，原样上抛。
        deps.tracer.mark_error("ServiceError", f"tool={name}")
        raise
    except Exception as exc:  # noqa: BLE001 —— 协议层异常统一按依赖不可用上报
        logger.warning("tool call failed name=%s err=%s", name, exc)
        raise ServiceError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "mcp tool call failed",
            detail=f"{type(exc).__name__}: {exc}"[:160],
        ) from exc

    structured = getattr(result, "structuredContent", None) or {}
    return ToolCallResponse(
        name=name,
        is_error=bool(getattr(result, "isError", False)),
        structured=dict(structured) if isinstance(structured, dict) else {},
        text=_text_of(result),
        request_id=request_id,
    )
