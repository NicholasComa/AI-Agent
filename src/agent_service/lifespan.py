"""服务生命周期：按固定顺序组装依赖，失败只降级不阻断启动。

组装顺序:

    运行准备（.env + 日志）-> 追踪 -> 配置 -> 会话存储 -> Qdrant 与知识库 ->
    LlmClient -> MCP 会话 -> 工作流图

每一步都独立捕获异常并写入 :class:`agent_service.schemas.DependencyState`：
- 必需依赖（会话存储、配置、Qdrant、模型接口、工作流）不可用时，服务照常
  启动，由 ``/health`` 与 ``/ready`` 给出 ``degraded`` 与具体原因；
- 可选依赖（MCP、追踪）默认不启用，避免无意义地拉起子进程。

真实连接信息沿用项目既有配置源：模型接口读 ``API_BASE_URL`` / ``MODEL_NAME``，
Qdrant 读 ``QDRANT_*``，不在服务层重复声明。``.env`` 只在启动阶段载入，导入本
模块不会改动进程环境——否则任何导入方（例如测试）都会被悄悄改写运行环境。
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from config import AppConfig, load_config
from graph import ChatFn, build_requirement_workflow, make_fake_chat
from jwipc_dev_mcp_server.client import connect_stdio
from llm_client import LlmClient
from logging_config import configure_logging
from observability import Tracer, build_tracer, traced_chat, traced_retriever
from rag.embeddings import get_embedding
from rag.knowledge_rag import (
    RETRIEVAL_STRATEGIES,
    JwipcKnowledgeRAG,
    build_qdrant_config,
    build_retriever,
)

from .deps import AgentServiceDeps
from .security import TokenBucketRateLimiter
from .session import SessionStore
from .settings import AgentServiceSettings

logger = logging.getLogger(__name__)

UNKNOWN_VERSION = "0.0.0"
"""读不到包版本时的缺省值。"""

_REQUIRED = True
_OPTIONAL = False

DEFAULT_RETRIEVAL_STRATEGY = "vector"
"""缺省检索策略。与第 6 周的既有行为保持一致——不配置就完全不变。"""

QDRANT_RETRIEVAL_STRATEGY_ENV = "QDRANT_RETRIEVAL_STRATEGY"
"""检索策略环境变量名。"""

QDRANT_COARSE_TOP_K_ENV = "QDRANT_COARSE_TOP_K"
"""粗排候选数环境变量名，供混合与重排策略使用。"""

QDRANT_TOP_CANDIDATES_ENV = "QDRANT_TOP_CANDIDATES"
"""重排候选数环境变量名。"""

DEFAULT_COARSE_TOP_K = 20
DEFAULT_TOP_CANDIDATES = 30


def _env_int(name: str, default: int) -> int:
    """读整型环境变量；缺失或非法时回退缺省值。

    非法值只回退不报错：检索参数配错属于「用默认值也能跑」的情形，没有理由
    让整个服务起不来。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("qdrant.env_int_invalid name=%s value=%r fallback=%s", name, raw, default)
        return default


def _prepare_runtime() -> None:
    """载入 ``.env`` 并按项目配置初始化结构化日志。

    ``.env`` 必须在读配置之前载入：``load_config`` 从进程环境变量取值。
    ``override=False`` 保证容器 / CI 注入的变量优先于文件内容。

    日志配置失败只降级为一行式 plain 格式并给出 WARNING——日志格式不统一
    远没有「服务起不来」严重。
    """
    load_dotenv()
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 —— 配置缺失是预期的降级路径
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        logger.warning("failed to configure structured logging: %s", exc)
        return
    configure_logging(config.log_level, config.log_format)


def _build_tracer(deps: AgentServiceDeps) -> None:
    """建立追踪器。

    追踪是旁路能力，不进 ``_REQUIRED`` 集合：无论远端上报是否可用，服务都应当
    正常提供服务，只有 ``required=False`` 的探活明细会记录实际后端。

    :func:`observability.build_tracer` 自身保证永不抛异常——SDK 缺失、凭据不全或
    远端初始化失败都会换成降级后端（本地 JSONL 或内存），因此这里不需要再包一层
    ``try``。降级原因写进探活明细，探针接口即可直接读出「当前到底在往哪写」。

    在组装链最前面调用还有一层顺序理由：释放钩子按注册逆序执行，最先注册的
    最后释放，追踪器因此能在其它资源关闭期间继续可用。
    """
    tracer = build_tracer()
    deps.tracer = tracer
    deps.add_closer(tracer.aclose)

    reason = tracer.degraded_reason
    detail = f"backend={tracer.backend_name} dir={tracer.config.trace_dir}"
    if reason:
        detail = f"{detail} degraded={reason}"
    deps.set_dependency(
        "observability",
        ready=reason is None,
        required=_OPTIONAL,
        detail=detail,
    )


def _brief(exc: BaseException, *, limit: int = 160) -> str:
    """把异常压缩成单行短说明。

    探针接口会把这些文字直接回给调用方，而原始异常常常带多行原始字节串，
    因此只取首行并截断，保留「异常类型 + 根因」这一最有用的部分。
    """
    text = str(exc).strip()
    first_line = text.splitlines()[0] if text else ""
    return f"{type(exc).__name__}: {(first_line or 'no message')[:limit]}"


def _make_chat_fn(client: LlmClient, tracer: Tracer | None = None) -> ChatFn:
    """把 :class:`LlmClient` 适配成工作流需要的对话函数。

    节点在 system 消息上挂了用于路由的 ``task`` 键，适配时只保留
    ``role`` 与 ``content`` 两个标准字段，避免把内部约定发给模型接口。

    Args:
        client: 模型客户端。
        tracer: 追踪门面；提供时给对话函数套一层 ``generation`` 埋点。

    Returns:
        供工作流调用的异步对话函数。
    """

    async def _chat(messages: list[dict[str, str]]) -> str:
        plain = [{"role": m["role"], "content": m["content"]} for m in messages]
        result = await client.chat(plain)
        return result.text

    if tracer is None:
        return _chat
    return traced_chat(_chat, tracer, model=getattr(client, "model", None))


def _prepare_session_store(deps: AgentServiceDeps) -> None:
    """建立会话与幂等存储。

    存储构造时会建目录、校验可写并从落盘文件恢复状态，因此这里同时充当
    ``session_store`` 的探活点。启动阶段不写任何临时探针文件：留下文件就必然
    要清理，等于给启动链路多引入一个失败点。
    """
    try:
        deps.sessions = SessionStore(deps.settings.session_dir)
    except OSError as exc:
        deps.set_dependency(
            "session_store",
            ready=False,
            required=_REQUIRED,
            detail=_brief(exc),
        )
        return
    stats = deps.sessions.stats()
    deps.set_dependency(
        "session_store",
        ready=True,
        required=_REQUIRED,
        detail=f"dir={deps.settings.session_dir.resolve()} sessions={stats['sessions']}",
    )


def _load_infra_config(deps: AgentServiceDeps) -> AppConfig | None:
    """读取模型接口与 Qdrant 的基础设施配置。"""
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 —— 配置缺失是预期的降级路径
        deps.set_dependency(
            "config",
            ready=False,
            required=_REQUIRED,
            detail=_brief(exc),
        )
        return None
    deps.set_dependency(
        "config",
        ready=True,
        required=_REQUIRED,
        detail=f"model={config.model_name} provider={config.provider}",
    )
    return config


def _build_knowledge_rag(deps: AgentServiceDeps) -> None:
    """连接 Qdrant 并建立知识库检索器。

    构造即建连并确保集合存在，因此这里同时充当 Qdrant 的探活点。

    检索器经 :func:`rag.knowledge_rag.build_retriever` 显式构造后再注入知识库，
    而不是让知识库用内部默认值自建：这样检索策略（向量 / 混合 / 重排）成为可配
    置项。默认策略仍是 ``vector``，与既有行为完全一致。

    这里**不挂追踪埋点**。RAG 层的检索器统一只提供 ``search()``，而追踪层的
    :class:`~observability.TracedRetriever` 包的是 ``retrieve()``；挂在这一层
    不会产出任何 span——知识库取的是内层检索器的 ``search``，正好绕过包装。埋点
    挂在真正的调用点外侧：服务路由见 :mod:`agent_service.routes.rag`，工作流见
    :func:`_build_workflow`，离线评测见 ``scripts/eval_week10_run.py``。
    """
    collection = deps.settings.collection_name
    try:
        embedder = get_embedding()
        qdrant_config = build_qdrant_config(collection, embedder)
        strategy = os.getenv(QDRANT_RETRIEVAL_STRATEGY_ENV, DEFAULT_RETRIEVAL_STRATEGY)
        if strategy not in RETRIEVAL_STRATEGIES:
            logger.warning(
                "qdrant.retrieval_strategy_invalid strategy=%s fallback=%s",
                strategy,
                DEFAULT_RETRIEVAL_STRATEGY,
            )
            strategy = DEFAULT_RETRIEVAL_STRATEGY
        retriever = build_retriever(
            embedder,
            qdrant_config,
            strategy=strategy,
            coarse_top_k=_env_int(QDRANT_COARSE_TOP_K_ENV, DEFAULT_COARSE_TOP_K),
            top_candidates=_env_int(QDRANT_TOP_CANDIDATES_ENV, DEFAULT_TOP_CANDIDATES),
        )
        # 埋点不挂在这里（理由见本函数 docstring），直接注入原始检索器。
        deps.rag = JwipcKnowledgeRAG(embedder, qdrant_config, retriever=retriever)
    except Exception as exc:  # noqa: BLE001 —— 库未启动时降级继续启动
        deps.set_dependency(
            "qdrant",
            ready=False,
            required=_REQUIRED,
            detail=_brief(exc),
        )
        return
    deps.set_dependency(
        "qdrant",
        ready=True,
        required=_REQUIRED,
        detail=(
            f"collection={collection} mode={os.getenv('QDRANT_MODE', 'local')} strategy={strategy}"
        ),
    )


def _build_llm(deps: AgentServiceDeps, config: AppConfig | None) -> ChatFn | None:
    """建立模型客户端，并返回工作流可用的对话函数。

    上游超时直接取服务级 ``request_timeout_seconds``：让统一的服务端超时
    先到期并返回明确的超时错误，避免上游 30 秒先超时后触发重试、把总延迟
    放大数倍。
    """
    if config is None:
        deps.set_dependency(
            "llm",
            ready=False,
            required=_REQUIRED,
            detail="skipped: infrastructure config is unavailable",
        )
        return None
    try:
        client = LlmClient(
            base_url=config.api_base_url,
            model=config.model_name,
            timeout_seconds=deps.settings.request_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 —— 密钥缺失等按降级处理
        deps.set_dependency(
            "llm",
            ready=False,
            required=_REQUIRED,
            detail=_brief(exc),
        )
        return None
    deps.llm = client
    deps.chat_fn = _make_chat_fn(client, deps.tracer)
    deps.add_closer(client.aclose)
    deps.backend = "real"
    deps.set_dependency(
        "llm",
        ready=True,
        required=_REQUIRED,
        detail=f"model={config.model_name} timeout={deps.settings.request_timeout_seconds}s",
    )
    return deps.chat_fn


async def _connect_mcp(deps: AgentServiceDeps) -> None:
    """可选建立 MCP 会话（stdio 传输）。"""
    if not deps.settings.enable_mcp:
        deps.set_dependency(
            "mcp",
            ready=False,
            required=_OPTIONAL,
            detail="disabled (AGENT_SERVICE_ENABLE_MCP=false)",
        )
        return

    script = deps.settings.mcp_server_script
    if not script.exists():
        deps.set_dependency(
            "mcp",
            ready=False,
            required=_OPTIONAL,
            detail=f"server script not found: {script}",
        )
        return

    stack = AsyncExitStack()
    try:
        session = await stack.enter_async_context(
            connect_stdio(sys.executable, [str(script), "--transport", "stdio"])
        )
    except Exception as exc:  # noqa: BLE001 —— MCP 是可选项，失败不影响主链路
        await stack.aclose()
        deps.set_dependency(
            "mcp",
            ready=False,
            required=_OPTIONAL,
            detail=_brief(exc),
        )
        return

    deps.mcp = session
    deps.add_closer(stack.aclose)
    deps.set_dependency(
        "mcp",
        ready=True,
        required=_OPTIONAL,
        detail=f"transport=stdio script={script}",
    )


def _build_workflow(deps: AgentServiceDeps, chat_fn: ChatFn | None) -> None:
    """编译需求分析工作流。

    模型接口不可用时回落到确定性对话函数，保证图结构仍可执行、测试仍可
    复现；此时 ``/health`` 的 ``backend`` 为 ``fake``。

    工作流拿到的知识库在这里套一层 ``retriever`` 埋点：服务路由会在
    :func:`agent_service.routes.rag.build_generator` 里按请求参数包一层，工作流
    没有那一层，不在这里挂就看不到检索明细。知识库自身不带埋点（原因见
    :func:`_build_knowledge_rag`）。``deps.rag`` 缺失时保持 ``None``，让图走它
    既有的「无检索」分支。
    """
    try:
        rag = None if deps.rag is None else traced_retriever(deps.rag, deps.tracer)
        deps.graph = build_requirement_workflow(chat_fn=chat_fn or make_fake_chat(), rag=rag)
    except Exception as exc:  # noqa: BLE001 —— 编译失败仍要让服务起来并报因
        deps.set_dependency(
            "workflow",
            ready=False,
            required=_REQUIRED,
            detail=_brief(exc),
        )
        return
    deps.set_dependency(
        "workflow",
        ready=True,
        required=_REQUIRED,
        detail=f"nodes=7 backend={deps.backend}",
    )


async def build_deps(
    *,
    settings: AgentServiceSettings | None = None,
    version: str = UNKNOWN_VERSION,
) -> AgentServiceDeps:
    """组装服务依赖。

    Args:
        settings: 服务配置；缺省从环境变量与 ``.env`` 读取。
        version: 应用版本，由应用工厂传入。

    Returns:
        依赖容器。任何一步失败都只体现在 ``dependencies`` 里，不抛异常。
    """
    _prepare_runtime()
    resolved = settings or AgentServiceSettings()
    deps = AgentServiceDeps.create(resolved, version=version)

    # 追踪器最先建立、最后释放：释放钩子按注册逆序执行，先注册才能让它在其它
    # 资源关闭期间仍然可用。它不依赖任何外部连接，无需参与降级流程。
    _build_tracer(deps)

    # 限流器不依赖任何外部连接，构造不会失败，因此不参与降级流程。
    deps.rate_limiter = TokenBucketRateLimiter(capacity=resolved.rate_limit_per_minute)

    _prepare_session_store(deps)
    config = _load_infra_config(deps)
    _build_knowledge_rag(deps)
    chat_fn = _build_llm(deps, config)
    await _connect_mcp(deps)
    _build_workflow(deps, chat_fn)

    degraded = deps.degraded_names
    logger.info(
        "agent_service.startup version=%s backend=%s degraded=%s",
        deps.version,
        deps.backend,
        degraded or "none",
    )
    if degraded:
        logger.warning("agent_service.degraded dependencies=%s", degraded)
    return deps


@asynccontextmanager
async def service_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期钩子：启动时组装依赖，关闭时按逆序释放。

    容器已由应用工厂注入（测试场景）时直接沿用，不再重复组装；
    关闭阶段无论释放是否成功都不向外抛异常。
    """
    existing: AgentServiceDeps | None = getattr(app.state, "deps", None)
    deps = existing if existing is not None else await build_deps(version=app.version)
    app.state.deps = deps
    try:
        yield
    finally:
        await deps.aclose()
        logger.info("agent_service.shutdown released=%d", len(deps.dependencies))


__all__ = [
    "UNKNOWN_VERSION",
    "build_deps",
    "service_lifespan",
]
