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

from fastapi import APIRouter, Request, Response
from sse_starlette.sse import EventSourceResponse

from observability import traced_chat, traced_retriever
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

TRACE_ID_HEADER = "X-Trace-Id"
"""响应头里的 trace 标识字段名；与 ``X-Request-ID`` 并列，便于按请求取记录。"""

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
    """包装检索器：记录指标，并产出一条 ``retriever`` span。

    指标与埋点写在**同一层包装器**里而不是各叠一层：两者都只需在调用前后各做
    一次动作，分两层会多一次属性透传，还会让「指标看到的调用次数」与「span
    数」存在错位的可能。

    命中判定与生成链路的拒答口径一致——召回非空**且** Top1 分数达到
    ``min_score``。只按「召回非空」统计会把低分拒答也算成命中，指标虚高。

    Args:
        deps: 依赖容器，取指标计数器与追踪门面。
        rag: 真实检索器，其 ``retrieve`` 签名与
            :meth:`rag.knowledge_rag.JwipcKnowledgeRAG.retrieve` 一致。
        min_score: 本次请求使用的拒答阈值，与生成器保持一致。
    """
    traced = traced_retriever(rag, deps.tracer, min_score=min_score)

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

    return _MeteredRetriever(traced)


def _metered_chat(deps: ServiceDeps, chat: Any) -> Any:
    """包装对话函数：按**实际调用次数**累加，重试会各计一次。

    同时套一层 ``generation`` 埋点：模型名取自依赖容器里的客户端，读不到就
    留空——埋点是旁路，不能因为它拿不到模型名而让对话调用失败。
    """

    traced = traced_chat(chat, deps.tracer, model=_model_name(deps))

    async def _call(messages: list[dict[str, str]]) -> str:
        deps.metrics.record_llm_call()
        return await traced(messages)

    return _call


def _model_name(deps: ServiceDeps) -> str | None:
    """尽力取出当前模型名；取不到返回 ``None``。"""
    try:
        return getattr(deps.llm, "model", None)
    except Exception:  # noqa: BLE001 —— 读不到模型名不影响主流程
        return None


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
    response: Response,
    deps: ServiceDeps,
) -> Any:
    """回答问题；``stream=true`` 时返回事件流。"""
    request_id = get_request_id(request)

    return await _traced_answer(
        deps,
        request=request,
        response=response,
        request_body=request_body,
        request_id=request_id,
    )


async def _traced_answer(
    deps: ServiceDeps,
    *,
    request: Request,
    response: Response,
    request_body: RagQueryRequest,
    request_id: str | None,
) -> Any:
    """在请求级 trace 里执行一次问答，并把 trace 标识写进响应头。

    trace 覆盖「组装生成器 → 产出响应」全程，**包括依赖解析**：``require()``
    在依赖缺失时抛 503，若把它放在 trace 之外，这类失败既不会留下 span，响应
    也不带 ``X-Trace-Id``，而排障恰恰最需要这两样。

    探针路径不建 trace（见 :data:`agent_service.app.PROBE_PATHS` 的处理），
    避免健康检查把追踪文件刷满。

    ``X-Trace-Id`` 写进注入的 :class:`fastapi.Response`，且**不放在 trace 体内**：
    声明式返回模型（``response_model=...``）实例上的属性不会被 FastAPI 读作
    响应头，而给它设未声明字段会抛 ``ValueError``。这个异常若发生在
    ``tracer.trace()`` 体内，会把本该成功的根 span 记成错误状态。
    """
    with deps.tracer.trace("rag.answer", request_id=request_id, top_k=request_body.top_k) as root:
        trace_id = root.trace_id
        generator = build_generator(request_body, deps)

        if request_body.stream:
            probe = make_disconnect_probe(request)
            sse = EventSourceResponse(
                rag_frames(
                    deps,
                    generator=generator,
                    question=request_body.question,
                    probe=probe,
                    request_id=request_id,
                )
            )
            # SSE 的响应头在构建时就确定；这是自建响应对象，直接写即可。
            sse.headers[TRACE_ID_HEADER] = trace_id
            return sse

        body = request_body.model_dump()
        replayed = idempotent_replay(deps, request, body)
        if replayed is not None:
            # 幂等回放直接返回自建的 JSONResponse，同样是写头而不是改模型。
            replayed.headers[TRACE_ID_HEADER] = trace_id
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

    response.headers[TRACE_ID_HEADER] = trace_id
    return result
