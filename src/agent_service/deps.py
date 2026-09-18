"""服务依赖容器与 FastAPI 依赖注入提供者。

路由函数只依赖 :class:`AgentServiceDeps` 一个对象，真实对象由
:mod:`agent_service.lifespan` 在启动阶段组装，测试则直接构造容器注入
替身。这样「谁来构造对象」只有一处答案，路由层不出现任何连接代码。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Request

from .errors import ErrorCode, ServiceError
from .metrics import MetricsRegistry
from .schemas import Backend, DependencyState
from .settings import AgentServiceSettings

logger = logging.getLogger(__name__)

Closer = Callable[[], Awaitable[None]]
"""异步释放钩子。容器关闭时按注册的逆序依次调用。"""

_LAYER_OF: dict[str, str] = {
    "rag": "qdrant",
    "llm": "llm",
    "chat_fn": "llm",
    "graph": "workflow",
    "mcp": "mcp",
    "sessions": "session_store",
}
"""容器字段名到探活层名的映射。

``require("rag")`` 取的是容器字段，而 :meth:`set_dependency` 记录的是语义层名
（``qdrant``）。映射一次，报错时才能带上「哪一层没起来」这个有用信息。
"""


@dataclass
class AgentServiceDeps:
    """服务运行期依赖集合。

    Attributes:
        settings: 服务级配置。
        version: 应用版本，用于探针回报。
        started_at: 启动时间（带时区）。
        backend: 后端模式，由模型接口是否就绪推导。
        llm: :class:`llm_client.LlmClient` 实例；未就绪时为 ``None``。
        chat_fn: 由 ``llm`` 适配出的对话函数（``messages -> text``），供 RAG
            生成与工作流共用；未就绪时为 ``None``。
        rag: 已就绪的知识库检索器；未就绪时为 ``None``。
        graph: 已编译的 LangGraph 工作流；未就绪时为 ``None``。
        mcp: 已握手的 MCP 会话；未启用或失败时为 ``None``。
        sessions: 会话与幂等存储；未就绪时为 ``None``。
        gate: 服务级并发闸门，容量取自 ``settings.max_concurrency``。
        rate_limiter: 按客户端计数的令牌桶；由启动阶段按配置建立，缺省为
            ``None``，首次限流检查时会按当前配置补建。
        metrics: 进程内指标计数器，供 ``/metrics-summary`` 读取。
        dependencies: 逐依赖探活结果，探针接口直接读取。
        closers: 释放钩子，按注册逆序执行。
    """

    settings: AgentServiceSettings
    version: str
    started_at: datetime
    backend: Backend = "fake"
    llm: Any = None
    chat_fn: Any = None
    rag: Any = None
    graph: Any = None
    mcp: Any = None
    sessions: Any = None
    gate: asyncio.Semaphore = field(init=False)
    rate_limiter: Any = None
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    dependencies: list[DependencyState] = field(default_factory=list)
    closers: list[Closer] = field(default_factory=list)

    def __post_init__(self) -> None:
        # asyncio.Semaphore 是惰性的：第一次 acquire 时才绑定当前事件循环，
        # 因此在同步构造阶段创建它是安全的。
        self.gate = asyncio.Semaphore(self.settings.max_concurrency)

    @classmethod
    def create(cls, settings: AgentServiceSettings, *, version: str) -> AgentServiceDeps:
        """构造一个尚未组装任何依赖的空容器。"""
        return cls(settings=settings, version=version, started_at=datetime.now(UTC))

    # ----- 依赖状态 -----

    def set_dependency(self, name: str, *, ready: bool, required: bool, detail: str) -> None:
        """写入或覆盖一个依赖的探活结果（同名只保留最新一条）。"""
        state = DependencyState(name=name, ready=ready, required=required, detail=detail)
        for index, existing in enumerate(self.dependencies):
            if existing.name == name:
                self.dependencies[index] = state
                return
        self.dependencies.append(state)

    def dependency(self, name: str) -> DependencyState | None:
        """按名字取依赖状态，不存在时返回 ``None``。"""
        for state in self.dependencies:
            if state.name == name:
                return state
        return None

    def require(self, name: str) -> Any:
        """取一个必需依赖，缺失时抛 :class:`ServiceError`。

        路由用本方法代替散落的 ``if deps.rag is None`` 判断，保证「依赖不可用」
        在所有接口上都返回同一个 503 与同一个错误码。
        """
        value = getattr(self, name, None)
        if value is None:
            state = self.dependency(_LAYER_OF.get(name, name))
            raise ServiceError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"{name} is not available",
                detail=state.detail if state is not None else "dependency was not initialized",
            )
        return value

    @property
    def degraded_names(self) -> list[str]:
        """未就绪的必需依赖名列表。"""
        return [state.name for state in self.dependencies if state.required and not state.ready]

    @property
    def ready(self) -> bool:
        """必需依赖是否全部就绪。"""
        return not self.degraded_names

    @property
    def uptime_seconds(self) -> float:
        """已运行秒数。"""
        return max((datetime.now(UTC) - self.started_at).total_seconds(), 0.0)

    # ----- 生命周期 -----

    def add_closer(self, closer: Closer) -> None:
        """注册一个释放钩子。"""
        self.closers.append(closer)

    async def aclose(self) -> None:
        """按注册逆序执行全部释放钩子。

        单个钩子失败只记日志，不中断后续释放流程，也绝不向外抛异常——
        关闭阶段的异常会掩盖真正的业务错误。
        """
        for closer in reversed(self.closers):
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 —— 关闭失败不影响其它资源释放
                logger.warning("closer failed: %s: %s", type(exc).__name__, exc)
        self.closers.clear()


def get_deps(request: Request) -> AgentServiceDeps:
    """FastAPI 依赖提供者：从应用状态取出容器。

    容器缺失说明生命周期钩子没有跑（例如直接调用路由函数），此时明确报
    ``dependency_unavailable``，而不是抛 ``AttributeError`` 变成 500。
    """
    deps: AgentServiceDeps | None = getattr(request.app.state, "deps", None)
    if deps is None:
        raise ServiceError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "service dependencies are not initialized",
            detail="lifespan did not run or failed before registering deps",
        )
    return deps


ServiceDeps = Annotated[AgentServiceDeps, Depends(get_deps)]
"""路由参数标注：``deps: ServiceDeps`` 即注入当前请求的依赖容器。"""
