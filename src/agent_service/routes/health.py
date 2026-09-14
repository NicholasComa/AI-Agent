"""运维探针路由：存活、就绪与依赖明细。

探针接口不参与认证，供容器 Healthcheck 与编排系统调用。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from ..deps import ServiceDeps
from ..errors import get_request_id
from ..schemas import (
    SERVICE_NAME,
    ServiceHealthResponse,
    ServiceReadyResponse,
)

router = APIRouter(tags=["ops"])


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
    """
    degraded = deps.degraded_names
    if degraded:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ServiceReadyResponse(
        status="degraded" if degraded else "ready",
        dependencies=deps.dependencies,
        request_id=get_request_id(request),
    )
