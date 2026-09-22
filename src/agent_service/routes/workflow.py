"""需求分析工作流路由。

把 :func:`graph.build_requirement_workflow` 编译出的图暴露为两个接口：

- ``POST /v1/workflow/requirement-analysis``：启动一次分析；
- ``POST /v1/workflow/{thread_id}/resume``：补充澄清信息后从挂起点续跑。

两条接口共用同一套「跑图 + 读快照」逻辑，区别只在入口负载是初始状态还是
:class:`langgraph.types.Command`。挂起判定统一取快照的 ``next``：非空即表示
图停在中断节点上，等待人工输入。

流式模式按节点推帧，是真正增量的：每个节点执行完推一帧 ``node``，中断推
``interrupt``，结束推 ``done``；帧里只带节点名与字段名，正文由结束帧统一给。
非流式模式等待全部执行完再一次性返回。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request, Response
from langgraph.types import Command
from sse_starlette.sse import EventSourceResponse

from observability import ObservabilityCallbackHandler

from ..deps import AgentServiceDeps, ServiceDeps
from ..errors import ErrorCode, ServiceError, get_request_id
from ..guards import guard_stream as _guard_stream
from ..guards import (
    idempotent_remember,
    idempotent_replay,
    make_disconnect_probe,
    request_slot,
)
from ..schemas import (
    WorkflowResumeRequest,
    WorkflowRunResponse,
    WorkflowStartRequest,
    WorkflowStatus,
)
from .rag import TRACE_ID_HEADER

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/workflow", tags=["workflow"])

_PATH = "/v1/workflow/requirement-analysis"


def _frame(payload: dict[str, Any]) -> dict[str, str]:
    return {"data": json.dumps(payload, ensure_ascii=False)}


def _thread_config(thread_id: str, handler: Any = None) -> dict[str, Any]:
    """组装线程配置，可选挂上节点埋点回调。

    ``callbacks`` 必须放在**顶层**而不是 ``configurable`` 里：LangGraph 从
    ``config["callbacks"]`` 取回调链，放进 ``configurable`` 不会生效，节点也就
    不会产出 ``chain`` span。

    Args:
        thread_id: 线程标识。
        handler: 节点埋点处理器；为 ``None`` 时行为与不加埋点完全一致。

    Returns:
        传给 ``astream`` / ``aget_state`` 的配置字典。
    """
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if handler is not None:
        config["callbacks"] = [handler]
    return config


def _update_frame(update: dict[str, Any]) -> dict[str, Any] | None:
    """把一个 LangGraph 更新事件翻成对外帧。

    ``stream_mode="updates"`` 的每个事件形如 ``{节点名: 局部 patch}``，中断则是
    ``{"__interrupt__": (Interrupt(...),)}``。局部 patch 的内容可能很长（报告
    正文），因此帧里只带节点名与字段名。
    """
    if "__interrupt__" in update:
        interrupts = update["__interrupt__"] or ()
        value = getattr(interrupts[0], "value", None) if interrupts else None
        questions = value.get("questions", []) if isinstance(value, dict) else []
        return {"type": "interrupt", "questions": list(questions)}
    for node, patch in update.items():
        keys = sorted(patch) if isinstance(patch, dict) else []
        return {"type": "node", "node": node, "keys": keys}
    return None


def _result_from_snapshot(
    snapshot: Any,
    *,
    thread_id: str,
    session_id: str | None,
    request_id: str | None,
) -> WorkflowRunResponse:
    """把图快照翻成响应模型。

    ``snapshot.next`` 非空表示图停在中断节点，此时没有 ``report``，调用方应依据
    ``clarification_questions`` 补充信息后再调 resume。
    """
    values: dict[str, Any] = dict(getattr(snapshot, "values", None) or {})
    pending = tuple(getattr(snapshot, "next", None) or ())
    status: WorkflowStatus = "awaiting_clarification" if pending else "completed"
    return WorkflowRunResponse(
        thread_id=thread_id,
        session_id=session_id,
        status=status,
        clarification_questions=list(values.get("clarification_questions") or []),
        report=values.get("report"),
        trace=list(values.get("trace") or []),
        rag_degraded=bool(values.get("rag_degraded")),
        errors=list(values.get("errors") or []),
        request_id=request_id,
    )


def _resolve_thread(
    deps: AgentServiceDeps,
    *,
    session_id: str | None,
    thread_id: str | None,
    rotate: bool,
) -> tuple[str, str | None]:
    """决定本次执行使用哪个线程。

    优先级：显式 ``thread_id`` > 会话绑定。启动一次新需求时轮换线程，避免
    LangGraph 的线程续用上一轮状态导致新需求读到旧的功能点与风险；续跑则必须
    复用会话已绑定的线程，否则会找不到挂起点。

    Raises:
        ServiceError: 调用方提供了 ``session_id`` 但会话存储未就绪。
    """
    if thread_id:
        return thread_id, session_id
    if session_id is None:
        return uuid.uuid4().hex, None
    store = deps.require("sessions")
    resolved = store.rotate_thread(session_id) if rotate else store.thread_id_for(session_id)
    return resolved, session_id


async def _drive(
    deps: AgentServiceDeps,
    *,
    graph: Any,
    payload: Any,
    thread_id: str,
    session_id: str | None,
    request_id: str | None,
    on_update: Any = None,
) -> tuple[WorkflowRunResponse, list[dict[str, Any]]]:
    """跑完一次执行并返回结果与更新事件序列。

    Args:
        on_update: 可选回调，每个更新事件调用一次；流式模式用它边跑边收集帧。

    Returns:
        ``(结果, 更新事件列表)``。
    """
    handler = ObservabilityCallbackHandler(deps.tracer, root_name="workflow")
    config = _thread_config(thread_id, handler)
    updates: list[dict[str, Any]] = []
    snapshot: Any = None
    try:
        # astream 同时满足两种模式：非流式只是把事件消费掉，流式用它逐节点推帧。
        async for update in graph.astream(payload, config=config, stream_mode="updates"):
            updates.append(update)
            if on_update is not None:
                await on_update(update)
        snapshot = await graph.aget_state(config)
    finally:
        # 挂起或客户端提前断开时会留下未收尾的节点，统一收尾成 unfinished 后
        # 投递，避免这些记录永远停在内存里。
        handler.close()
    result = _result_from_snapshot(
        snapshot,
        thread_id=thread_id,
        session_id=session_id,
        request_id=request_id,
    )
    if session_id is not None and deps.sessions is not None:
        deps.sessions.record_turn(session_id, thread_id=thread_id)
    return result, updates


async def _assert_resumable(graph: Any, thread_id: str) -> None:
    """确认线程确实停在中断点上。

    没有挂起点时 ``Command(resume=...)`` 的语义是未定义的（可能从空状态开始跑，
    也可能直接报错），因此在入口就给出明确的 422，而不是让调用方面对一个含义
    模糊的结果。
    """
    snapshot = await graph.aget_state(_thread_config(thread_id))
    if not tuple(getattr(snapshot, "next", None) or ()):
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            "no pending clarification for this thread",
            detail=f"thread_id={thread_id} has no suspended node to resume",
        )


async def workflow_frames(
    deps: AgentServiceDeps,
    *,
    graph: Any,
    payload: Any,
    thread_id: str,
    session_id: str | None,
    probe: Any,
    request_id: str | None,
) -> AsyncIterator[dict[str, str]]:
    """SSE 事件生成器：按节点推帧，结束时给完整结果。"""
    frames: list[dict[str, Any]] = []

    async def _collect(update: dict[str, Any]) -> None:
        frame = _update_frame(update)
        if frame is not None:
            frames.append(frame)

    try:
        async with request_slot(deps):
            result, _ = await _drive(
                deps,
                graph=graph,
                payload=payload,
                thread_id=thread_id,
                session_id=session_id,
                request_id=request_id,
                on_update=_collect,
            )
    except ServiceError as exc:
        yield _frame(
            {
                "type": "error",
                "code": exc.code.value,
                "message": exc.message,
                "detail": exc.detail,
                "request_id": request_id,
            }
        )
        return

    for frame in frames:
        if await _guard_stream(probe, path=_PATH):
            return
        yield _frame(frame)
    yield _frame({"type": "done", **result.model_dump(mode="json")})


async def _run_once(
    deps: AgentServiceDeps,
    *,
    graph: Any,
    payload: Any,
    thread_id: str,
    session_id: str | None,
    request_id: str | None,
) -> WorkflowRunResponse:
    async with request_slot(deps):
        result, _ = await _drive(
            deps,
            graph=graph,
            payload=payload,
            thread_id=thread_id,
            session_id=session_id,
            request_id=request_id,
        )
    return result


def _event_stream(
    *,
    request: Request,
    deps: AgentServiceDeps,
    graph: Any,
    payload: Any,
    thread_id: str,
    session_id: str | None,
    request_id: str | None,
    trace_id: str | None = None,
) -> EventSourceResponse:
    """构造工作流的 SSE 响应，带上 trace 标识头。"""
    response = EventSourceResponse(
        workflow_frames(
            deps,
            graph=graph,
            payload=payload,
            thread_id=thread_id,
            session_id=session_id,
            probe=make_disconnect_probe(request),
            request_id=request_id,
        )
    )
    if trace_id:
        response.headers[TRACE_ID_HEADER] = trace_id
    return response


def _with_trace_header(response: Any, trace_id: str) -> Any:
    """给自建响应对象补 ``X-Trace-Id`` 头。

    只用于已经构造好的响应（幂等回放）：声明式返回模型（``response_model``）的
    实例不接受未声明字段，写它会抛 ``ValueError``，因此普通返回路径改用注入的
    :class:`fastapi.Response`。头名与取值口径统一取自 :mod:`routes.rag`。
    """
    response.headers[TRACE_ID_HEADER] = trace_id
    return response


@router.post(
    "/requirement-analysis",
    response_model=WorkflowRunResponse,
    summary="需求分析",
    description=(
        "对需求文本做分类、功能点、检索、风险与测试点分析；"
        "关键信息不足时挂起并列出澄清问题。传 stream=true 按节点推帧。"
    ),
)
async def start(
    request_body: WorkflowStartRequest,
    request: Request,
    response: Response,
    deps: ServiceDeps,
) -> Any:
    """启动一次需求分析。"""
    request_id = get_request_id(request)
    graph = deps.require("graph")
    thread_id, session_id = _resolve_thread(
        deps,
        session_id=request_body.session_id,
        thread_id=request_body.thread_id,
        rotate=True,
    )
    payload = {"requirement_text": request_body.requirement_text}

    with deps.tracer.trace(
        "workflow.start",
        request_id=request_id,
        thread_id=thread_id,
        session_id=session_id,
    ) as root:
        trace_id = root.trace_id
        if request_body.stream:
            return _event_stream(
                request=request,
                deps=deps,
                graph=graph,
                payload=payload,
                thread_id=thread_id,
                session_id=session_id,
                request_id=request_id,
                trace_id=trace_id,
            )

        body = request_body.model_dump()
        replayed = idempotent_replay(deps, request, body)
        if replayed is not None:
            return _with_trace_header(replayed, trace_id)

        result = await _run_once(
            deps,
            graph=graph,
            payload=payload,
            thread_id=thread_id,
            session_id=session_id,
            request_id=request_id,
        )
        idempotent_remember(
            deps,
            request,
            body,
            status_code=200,
            payload=result.model_dump(mode="json"),
        )

    # 写头放在 trace 体外：既避开「给模型实例设未声明字段」的报错，也避免该
    # 报错把本该成功的根 span 标成错误。
    response.headers[TRACE_ID_HEADER] = trace_id
    return result


@router.post(
    "/{thread_id}/resume",
    response_model=WorkflowRunResponse,
    summary="补充澄清信息后继续",
    description="把人工补充的答案送回挂起的线程，从断点继续跑完。",
)
async def resume(
    thread_id: str,
    request_body: WorkflowResumeRequest,
    request: Request,
    response: Response,
    deps: ServiceDeps,
) -> Any:
    """人机确认之后的续跑。"""
    request_id = get_request_id(request)
    graph = deps.require("graph")
    await _assert_resumable(graph, thread_id)
    payload = Command(resume=list(request_body.answers))

    with deps.tracer.trace(
        "workflow.resume",
        request_id=request_id,
        thread_id=thread_id,
        answer_count=len(request_body.answers),
    ) as root:
        trace_id = root.trace_id
        if request_body.stream:
            return _event_stream(
                request=request,
                deps=deps,
                graph=graph,
                payload=payload,
                thread_id=thread_id,
                session_id=None,
                request_id=request_id,
                trace_id=trace_id,
            )

        result = await _run_once(
            deps,
            graph=graph,
            payload=payload,
            thread_id=thread_id,
            session_id=None,
            request_id=request_id,
        )

    response.headers[TRACE_ID_HEADER] = trace_id
    return result
