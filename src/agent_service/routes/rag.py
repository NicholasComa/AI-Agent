"""RAG 问答路由。

复用 :class:`rag.generator.RagGenerator`，服务层只做三件事：把线协议参数翻成
生成器参数、在并发闸门与总时限内执行、按 ``?stream=true`` 决定一次性返回还是
逐帧推送。

关于流式语义：检索与生成是一次 LLM 调用，本身不产生增量 token，因此 ``delta``
帧推送的是**已生成的答案文本**分片，不是模型实时输出。这样 SSE 通道的契约
（帧顺序、取消、超时、背压）与其它接口保持一致，后续把生成换成真流式时客户端
无需改动。工作流接口的流式是真正按节点增量的。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from rag.generator import RagAnswer, RagGenerator

from ..deps import ServiceDeps
from ..errors import ServiceError, get_request_id
from ..guards import guard_stream as _guard_stream
from ..guards import (
    idempotent_remember,
    idempotent_replay,
    make_disconnect_probe,
    request_slot,
)
from ..schemas import RagQueryRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/rag", tags=["rag"])

DELTA_CHUNK_SIZE = 24
"""``delta`` 帧的文本分片长度（字符）。"""

_PATH = "/v1/rag/answer"


def _frame(payload: dict[str, Any]) -> dict[str, str]:
    """把一帧事件序列化成 SSE 数据行。"""
    return {"data": json.dumps(payload, ensure_ascii=False)}


def build_generator(request_body: RagQueryRequest, deps: ServiceDeps) -> RagGenerator:
    """按请求参数组装生成器；依赖缺失时抛 503。"""
    return RagGenerator(
        deps.require("rag"),
        deps.require("chat_fn"),
        top_k=request_body.top_k,
        min_score=request_body.min_score,
    )


async def rag_frames(
    deps: ServiceDeps,
    *,
    generator: RagGenerator,
    question: str,
    probe: Any,
    request_id: str | None,
) -> AsyncIterator[dict[str, str]]:
    """SSE 事件生成器。

    独立成函数，便于直接测试帧序列与取消行为。帧顺序固定为
    ``delta* → citations → done``；失败时改为 ``error → done``，保证客户端
    总能收到结束帧。
    """
    try:
        # 槽位必须覆盖整段流式推送：否则生成阶段占用并发额度而推送阶段不占，
        # 闸门就形同虚设。
        async with request_slot(deps):
            answer = await generator.answer(question)
            for start in range(0, len(answer.answer), DELTA_CHUNK_SIZE):
                if await _guard_stream(probe, path=_PATH):
                    return
                yield _frame(
                    {"type": "delta", "text": answer.answer[start : start + DELTA_CHUNK_SIZE]}
                )
            if await _guard_stream(probe, path=_PATH):
                return
            yield _frame(
                {
                    "type": "citations",
                    "items": [citation.model_dump() for citation in answer.citations],
                }
            )
            yield _frame(
                {
                    "type": "done",
                    "has_answer": answer.has_answer,
                    "rejected_reason": answer.rejected_reason,
                    "request_id": request_id,
                }
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
        yield _frame({"type": "done", "has_answer": False, "request_id": request_id})


@router.post(
    "/answer",
    response_model=RagAnswer,
    summary="知识库问答",
    description="依据内部知识库回答并给出引用；资料不足时明确拒答。传 stream=true 改用 SSE。",
    responses={
        200: {
            "content": {
                "text/event-stream": {
                    "schema": {
                        "type": "string",
                        "description": "SSE 帧序列：delta / citations / done；失败时为 error / done",
                    }
                }
            }
        }
    },
)
async def answer(
    request_body: RagQueryRequest,
    request: Request,
    deps: ServiceDeps,
) -> Any:
    """回答问题；``stream=true`` 时返回事件流。"""
    request_id = get_request_id(request)
    generator = build_generator(request_body, deps)

    if request_body.stream:
        probe = make_disconnect_probe(request)
        return EventSourceResponse(
            rag_frames(
                deps,
                generator=generator,
                question=request_body.question,
                probe=probe,
                request_id=request_id,
            )
        )

    body = request_body.model_dump()
    replayed = idempotent_replay(deps, request, body)
    if replayed is not None:
        return replayed

    async with request_slot(deps):
        result = await generator.answer(request_body.question)

    idempotent_remember(
        deps,
        request,
        body,
        status_code=200,
        payload=result.model_dump(mode="json"),
    )
    return result
