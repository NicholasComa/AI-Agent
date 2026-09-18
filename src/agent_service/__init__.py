"""Agent 服务包：把 RAG、工作流与 MCP 组合为可部署后端服务。

模块分工::

    app.py       应用工厂与异常处理器
    deps.py      依赖容器与 FastAPI 依赖注入提供者
    lifespan.py  启动/关闭顺序与依赖组装
    settings.py  服务级配置（AGENT_SERVICE_* 环境变量）
    errors.py    错误码与统一错误信封
    schemas.py   线协议模型
    metrics.py   进程内指标计数器与采集中间件
    session.py   会话绑定与幂等缓存（落盘）
    guards.py    并发闸门、请求总时限、断开检测与幂等回放
    security.py  Bearer 认证、令牌桶限流与请求体大小限制
    routes/      接口层，按能力拆分

导入本包不会建立网络连接：所有真实对象都在生命周期钩子里构造。
"""

from .app import create_agent_service_app
from .deps import AgentServiceDeps, get_deps
from .errors import ErrorCode, ServiceError
from .lifespan import build_deps, service_lifespan
from .metrics import MetricsASGIMiddleware, MetricsRegistry
from .security import BodySizeLimitMiddleware, TokenBucketRateLimiter
from .session import IdempotentEntry, SessionRecord, SessionStore
from .settings import AgentServiceSettings

__all__ = [
    "AgentServiceDeps",
    "AgentServiceSettings",
    "BodySizeLimitMiddleware",
    "ErrorCode",
    "IdempotentEntry",
    "MetricsASGIMiddleware",
    "MetricsRegistry",
    "ServiceError",
    "SessionRecord",
    "SessionStore",
    "TokenBucketRateLimiter",
    "build_deps",
    "create_agent_service_app",
    "get_deps",
    "service_lifespan",
]
