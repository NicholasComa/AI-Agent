"""运维探针路由：存活、就绪与指标摘要。

三个接口分工明确，回答三个不同的问题：

* ``/health`` —— 进程还活着吗？只读内存状态，不触碰任何外部依赖，因此
  依赖全挂时它仍然 200。编排系统据此区分「进程死了」与「依赖坏了」。
* ``/ready`` —— 能接业务流量吗？逐依赖判定，必需依赖未就绪时返回 503。
  它同时刷新会变化的关键量（集合片段数、MCP 工具数），使运维不必另开接口。
* ``/metrics-summary`` —— 这段时间跑得怎么样？读进程内计数器，零成本。

探针接口不参与认证，供容器 Healthcheck 与编排系统调用。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response, status

from ..deps import ServiceDeps
from ..errors import get_request_id
from ..schemas import (
    SERVICE_NAME,
    ServiceHealthResponse,
    ServiceMetricsSummaryResponse,
    ServiceReadyResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])


_READY_SCAN_ORDER = (
    "observability",
    "session_store",
    "config",
    "qdrant",
    "llm",
    "mcp",
    "workflow",
)
"""``/ready`` 响应里依赖的展示顺序。

固定顺序让「依赖链是否完整」可以用一次字符串比较验证——评测脚本与第 10 周的
数据集都能直接断言，不必写集合比较。顺序本身也反映真实组装顺序（见
:mod:`agent_service.lifespan`）。

``observability`` 排在最前，因为组装链里 :func:`agent_service.lifespan._build_tracer`
是第一个被调用的——释放钩子按注册逆序执行，最先注册的追踪器因此能活到其它资源
全部关闭之后，这一位置不能在响应里被打乱。
"""


def _refresh_qdrant_detail(deps: ServiceDeps) -> None:
    """把集合当前片段数刷进 ``qdrant`` 的明细。

    启动时的明细只记了集合名，而 ``points_count`` 是判断「卷是否还在」最直接的
    数字——持久化测试正是靠它对比重启前后是否一致。这里在每次 ``/ready`` 调用时
    重读一次，读取失败只更新明细文字，不把依赖翻成未就绪。
    """
    state = deps.dependency("qdrant")
    rag = deps.rag
    if state is None or not state.ready or rag is None:
        return
    try:
        points = rag.count()
    except Exception as exc:  # noqa: BLE001 —— 明细读取失败不该让探针翻脸
        logger.debug("ready points_count failed: %s", exc)
        deps.set_dependency(
            "qdrant",
            ready=True,
            required=state.required,
            detail=f"{state.detail} points_count=unavailable",
        )
        return
    base = state.detail.split(" points_count=")[0]
    deps.set_dependency(
        "qdrant",
        ready=True,
        required=state.required,
        detail=f"{base} points_count={points}",
    )


async def _refresh_mcp_detail(deps: ServiceDeps) -> None:
    """把 MCP 工具数刷进 ``mcp`` 的明细。

    工具数是判断 MCP Server 是否真的握手成功的可核验证据：会话对象存在但工具
    列表为空，说明子进程起来了却没注册成功。

    仅在启动阶段已标记就绪时刷新——未启用 MCP 时不应该为此拉起一次往返调用。
    """
    state = deps.dependency("mcp")
    session = deps.mcp
    if state is None or not state.ready or session is None:
        return
    try:
        listing = await session.list_tools()
        tools = len(getattr(listing, "tools", None) or ())
    except Exception as exc:  # noqa: BLE001 —— 同上，明细失败不改变就绪结论
        logger.debug("ready tool count failed: %s", exc)
        return
    base = state.detail.split(" tools=")[0]
    deps.set_dependency(
        "mcp",
        ready=True,
        required=state.required,
        detail=f"{base} tools={tools}",
    )


def _ordered_dependencies(deps: ServiceDeps) -> list[Any]:
    """按固定顺序排列依赖明细，未知项追加在末尾。"""
    by_name = {state.name: state for state in deps.dependencies}
    ordered = [by_name[name] for name in _READY_SCAN_ORDER if name in by_name]
    ordered.extend(state for state in deps.dependencies if state.name not in _READY_SCAN_ORDER)
    return ordered


@router.get(
    "/health",
    response_model=ServiceHealthResponse,
    summary="存活探针",
    description="回答「进程是否还活着」，并回报版本、后端模式与运行时长。",
)
async def health(
    request: Request,
    deps: ServiceDeps,
) -> ServiceHealthResponse:
    """返回进程级健康状态。"""
    return ServiceHealthResponse(
        status="ok" if deps.ready else "degraded",
        service=SERVICE_NAME,
        version=deps.version,
        backend=deps.backend,
        started_at=deps.started_at.isoformat(),
        uptime_seconds=round(deps.uptime_seconds, 3),
        request_id=get_request_id(request),
    )


@router.get(
    "/ready",
    response_model=ServiceReadyResponse,
    summary="就绪探针",
    description="逐依赖给出探活结论；必需依赖未就绪时返回 503，并说明是哪一个。",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "必需依赖未就绪"}},
)
async def ready(
    request: Request,
    response: Response,
    deps: ServiceDeps,
) -> ServiceReadyResponse:
    """返回逐依赖的就绪明细。

    必需依赖未就绪时状态码置为 503，但响应体保持完整，调用方可以直接从
    ``dependencies`` 定位故障层，不必翻服务日志。

    本接口会顺带刷新两类会变化的数字（Qdrant 片段数、MCP 工具数）。它们属于
    「观测」而非「判定」：刷新失败不改动 ``ready`` 结论，避免探针自身成为
    新的故障源。
    """
    _refresh_qdrant_detail(deps)
    await _refresh_mcp_detail(deps)

    degraded = deps.degraded_names
    if degraded:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ServiceReadyResponse(
        status="degraded" if degraded else "ready",
        dependencies=_ordered_dependencies(deps),
        request_id=get_request_id(request),
    )


@router.get(
    "/metrics-summary",
    response_model=ServiceMetricsSummaryResponse,
    summary="指标摘要",
    description=(
        "返回进程内累计的请求量、延迟分位、模型调用、检索命中率与错误码分布。"
        "计数随进程重启归零，反映的是当前实例的运行状况。"
    ),
)
async def metrics_summary(
    request: Request,
    deps: ServiceDeps,
) -> ServiceMetricsSummaryResponse:
    """导出指标快照。

    只读内存计数器，不做任何外部调用，因此可以放心被高频抓取。
    """
    summary = deps.metrics.summary()
    return ServiceMetricsSummaryResponse(
        service=SERVICE_NAME,
        uptime_seconds=round(deps.uptime_seconds, 3),
        request_id=get_request_id(request),
        **summary,
    )
