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
    """按请求参数组装生成器；依赖缺失时抛 503。

    这里把检索器与对话函数各包一层计数钩子，而不是在 :class:`RagGenerator`
    内部埋点——生成器属于 RAG 领域层，不该知道 HTTP 服务的指标口径。钩子只
    做累加，不改变返回结构，因此对拒答、重试、引用校验等分支完全透明。
    """
    rag = _metered_retriever(deps, deps.require("rag"), min_score=request_body.min_score)
    chat = _metered_chat(deps, deps.require("chat_fn"))
    return RagGenerator(
        rag,
        chat,
        top_k=request_body.top_k,
        min_score=request_body.min_score,
    )


def _metered_retriever(deps: ServiceDeps, rag: Any, *, min_score: float) -> Any:
    """包装检索器：记录检索次数与命中率。

    命中判定与生成链路的拒答口径一致——召回非空**且** Top1 分数达到
    ``min_score``。只按「召回非空」统计会把低分拒答也算成命中，指标虚高。

    Args:
        deps: 依赖容器，取指标计数器。
        rag: 真实检索器，其 ``retrieve`` 签名与
            :meth:`rag.knowledge_rag.JwipcKnowledgeRAG.retrieve` 一致。
        min_score: 本次请求使用的拒答阈值，与生成器保持一致。
    """

    class _MeteredRetriever:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def retrieve(self, query: str, top_k: int = 3, **kwargs: Any) -> Any:
            results = self._inner.retrieve(query, top_k=top_k, **kwargs)
            hit = bool(results) and results[0].score >= min_score
            deps.metrics.record_rag_query(hit=hit)
            return results

        def __getattr__(self, name: str) -> Any:
            # 其余属性（如 count / index）原样透传，避免包装层变成窄接口。
            return getattr(self._inner, name)

    return _MeteredRetriever(rag)


def _metered_chat(deps: ServiceDeps, chat: Any) -> Any:
    """包装对话函数：按**实际调用次数**累加，重试会各计一次。"""

    async def _call(messages: list[dict[str, str]]) -> str:
        deps.metrics.record_llm_call()
        return await chat(messages)

    return _call


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
