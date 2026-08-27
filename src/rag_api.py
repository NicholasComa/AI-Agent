"""RAG 问答 API：把 :class:`RagGenerator` 暴露为 HTTP 端点。

本模块只做 HTTP 层编排，复用既有组件，不重复实现业务逻辑：

- 请求模型 :class:`QueryReq` 定义线协议（``question`` / ``top_k`` /
  ``min_score``），``extra="forbid"`` 拒绝未知字段；
- 响应直接复用 :class:`rag.generator.RagAnswer`（含 answer /
  has_answer / citations / confidence / rejected_reason），保证
  「答案可追溯、无依据可拒答」的契约与离线链路完全一致；
- LLM 调用通过 ``chat_factory`` 注入（与 :class:`ChatFn` 解耦），
  测试注入假实现即可跑通，真实链路由调用方把
  :class:`llm_client.LlmClient` 适配成 :class:`ChatFn`。

用法（测试）::

    app = create_rag_app(rag_factory=..., chat_factory=...)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
        r = await c.post("/rag/query", json={"question": "..."})
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from llm_client import LlmClient
from rag.generator import ChatFn, RagAnswer, RagGenerator
from rag.knowledge_rag import JwipcKnowledgeRAG

DEFAULT_COLLECTION = "jwipc_knowledge"
"""RAG API 默认使用的 Qdrant 集合名（与导入脚本一致）。"""

RagFactory = Callable[[], JwipcKnowledgeRAG]
"""无参可调用对象，产出已就绪的 :class:`JwipcKnowledgeRAG`（已导入知识库）。"""

ChatFactory = Callable[[], ChatFn]
"""无参可调用对象，产出一次问答可用的 :class:`ChatFn`。"""


class QueryReq(BaseModel):
    """``POST /rag/query`` 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, max_length=4000, description="用户问题，非空")
    top_k: int = Field(3, ge=1, le=20, description="召回片段数")
    min_score: float = Field(0.3, ge=0.0, le=1.0, description="Top1 相似度拒答阈值")


def create_rag_app(
    *,
    rag_factory: RagFactory,
    chat_factory: ChatFactory,
    title: str = "jwipc-rag-api",
) -> FastAPI:
    """构建 RAG 问答应用。

    Args:
        rag_factory: 产出已导入知识库的 :class:`JwipcKnowledgeRAG`。
            ``None`` 场景不存在 —— 知识库如何构建/导入由调用方决定
            （真实链路注入连接 Docker Qdrant 的实例，测试注入内存库）。
        chat_factory: 产出 :class:`ChatFn` 的工厂；真实链路把
            :class:`llm_client.LlmClient` 包成 ``async (messages) -> str``，
            测试注入假实现。
        title: 应用标题。

    Returns:
        注册了 ``POST /rag/query`` 的 :class:`FastAPI` 实例。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """启动时构建 RAG 与 ChatFn 工厂，挂到 ``app.state`` 供路由使用。"""
        app.state.rag = rag_factory()
        app.state.chat_factory = chat_factory
        yield

    app = FastAPI(title=title, lifespan=lifespan)

    @app.post("/rag/query", response_model=RagAnswer)
    async def rag_query(req: QueryReq) -> RagAnswer:
        """对 ``question`` 执行检索增强生成，返回带引用答案或拒答结果。"""
        rag: JwipcKnowledgeRAG = app.state.rag
        chat: ChatFn = app.state.chat_factory()
        generator = RagGenerator(rag, chat, top_k=req.top_k, min_score=req.min_score)
        return await generator.answer(req.question)

    return app


def _default_rag_factory() -> JwipcKnowledgeRAG:
    """真实链路：从 ``.env`` 构建连接 Docker Qdrant 的 RAG 实例。

    仅在服务启动（lifespan）时调用一次，惰性导入依赖并连接 Qdrant；
    导入本模块不会触发任何网络/磁盘 I/O。
    """
    from dotenv import load_dotenv

    from rag.embeddings import get_embedding
    from rag.knowledge_rag import build_qdrant_config

    load_dotenv()
    embedder = get_embedding()
    cfg = build_qdrant_config(DEFAULT_COLLECTION, embedder)
    return JwipcKnowledgeRAG(embedder, cfg)


# 跨请求复用的单例 LLM 客户端，避免每次请求新建连接。
_default_llm: LlmClient | None = None


def _default_chat_factory() -> ChatFn:
    """真实链路：把 :class:`llm_client.LlmClient` 适配成 :class:`ChatFn`。

    仅在服务启动（lifespan）时调用一次；复用同一个 LLM 客户端，不随
    请求重建。
    """
    from dotenv import load_dotenv

    from config import load_config

    global _default_llm
    load_dotenv()
    if _default_llm is None:
        app_cfg = load_config()
        _default_llm = LlmClient(
            base_url=app_cfg.api_base_url,
            model=app_cfg.model_name,
            timeout_seconds=app_cfg.timeout_seconds,
        )
    llm = _default_llm

    async def chat(messages: list[dict[str, str]]) -> str:
        result = await llm.chat(
            messages,
            extra_body={"response_format": {"type": "json_object"}, "temperature": 0.0},
        )
        return result.text

    return chat


# 供 ``fastapi dev src/rag_api.py`` 自动发现的模块级入口（与 ``src/app.py`` 一致）。
# 工厂只在服务启动时执行，导入本模块不会触发任何网络/磁盘 I/O。
app = create_rag_app(
    rag_factory=_default_rag_factory,
    chat_factory=_default_chat_factory,
)


# 类型别名导出（便于调用方按名称引用注入契约）
__all__ = [
    "ChatFactory",
    "DEFAULT_COLLECTION",
    "QueryReq",
    "RagFactory",
    "app",
    "create_rag_app",
]
