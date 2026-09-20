"""``src/agent_service`` 测试的共享替身与工具函数。

放在独立模块而不是 conftest 的原因与 ``tests/fake_models.py`` 一致：``tests/``
不是 Python 包，跨文件复用要靠模块导入；而 ``conftest`` 这个名字已被
``tests/conftest.py`` 占用，同名模块会冲突。

三个替身各管一段：

- :class:`ServiceChat` 同时满足 RAG 与工作流的对话契约，让两条链路共用一份假
  模型，避免为每个接口各写一套；
- :class:`FakeMcpSession` 实现 MCP 会话的 ``list_tools`` / ``call_tool``，使工具
  路由可脱离子进程测试；
- :func:`build_deps` 组装依赖容器，测试按需替换其中任意一项。

其余常量与 :func:`build_rag` 供夹具与用例共用。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from agent_service import (
    AgentServiceDeps,
    AgentServiceSettings,
    SessionStore,
)
from graph import build_requirement_workflow
from graph.fakes import fake_chat
from observability import ObservabilityConfig, Tracer, build_tracer
from rag.embeddings import FakeEmbedding
from rag.knowledge_rag import JwipcKnowledgeRAG
from rag.qdrant_store import QdrantConfig

DIM = 1024
"""假嵌入维度，与 mxbai-embed-large 的输出一致。"""

DOC_ZH = "Qdrant 是向量数据库，支持语义检索。"
"""测试语料的单句文档。"""

DOC_CHUNK_ID = "qdrant_intro.md#0"
"""语料唯一片段 ID：引用白名单只认它，构造假模型回答时需要。"""

QUERY_HIT = "Qdrant 是什么数据库？"
"""能命中语料的查询。"""

QUERY_MISS = "今天天气怎么样？"
"""与语料无共同 token 的查询，用于验证低分拒答。"""

NORMAL_REQUIREMENT = "开发一个电商网站，包含商品浏览、购物车与支付功能。"
"""完整需求，走正常路径。"""

AMBIGUOUS_REQUIREMENT = "帮我做个东西"
"""信息不足的需求：不含领域词且长度较短，假模型据此判低置信度并要求澄清。"""


class ServiceChat:
    """同时服务 RAG 与工作流的假对话函数。

    判定依据是消息内容而非调用顺序：RAG 的系统提示词里含「参考资料」字样，这类
    请求按检索增强生成契约返回带引用的 JSON；其余交给
    :func:`graph.fakes.fake_chat` 的 ``task`` 路由，保证工作流各节点拿到各自字段
    的结构化 JSON。

    Args:
        chunk_id: 允许被引用的片段 ID；引用白名单只认它。
        answer: 回答正文。
        delay_seconds: 每次调用前的人为延迟，用于构造超时场景。
    """

    def __init__(
        self,
        chunk_id: str,
        *,
        answer: str = "Qdrant 是一个向量数据库。",
        delay_seconds: float = 0.0,
    ) -> None:
        self.chunk_id = chunk_id
        self.answer_text = answer
        self.delay_seconds = delay_seconds
        self.calls: list[list[dict[str, str]]] = []

    async def __call__(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        joined = "\n".join(message.get("content", "") for message in messages)
        if "参考资料" in joined:
            return json.dumps(
                {
                    "answer": self.answer_text,
                    "has_answer": True,
                    "citations": [
                        {"source": "qdrant_intro.md", "chunk_id": self.chunk_id, "quote": DOC_ZH}
                    ],
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            )
        return await fake_chat(messages)

    @property
    def call_count(self) -> int:
        return len(self.calls)


@dataclass
class FakeTool:
    """MCP 工具元信息替身。"""

    name: str
    description: str = ""
    inputSchema: dict[str, Any] = field(default_factory=dict)  # noqa: N815 - 对齐 MCP SDK 字段名


@dataclass
class FakeContent:
    """MCP 工具返回的文本内容块替身。"""

    text: str


@dataclass
class FakeCallResult:
    """MCP 工具调用结果替身。"""

    structuredContent: dict[str, Any] = field(default_factory=dict)  # noqa: N815 - 对齐 SDK 字段名
    isError: bool = False  # noqa: N815 - 对齐 MCP SDK 字段名
    content: list[FakeContent] = field(default_factory=list)


@dataclass
class FakeToolList:
    """MCP 工具清单替身。"""

    tools: list[FakeTool] = field(default_factory=list)


class FakeMcpSession:
    """MCP 会话替身。

    Args:
        tools: 工具清单。
        results: 工具名到调用结果的映射；未列出的工具返回 ``isError=True``，与
            真实 Server 对未知工具的行为一致。
    """

    def __init__(
        self,
        tools: list[FakeTool] | None = None,
        results: dict[str, FakeCallResult] | None = None,
    ) -> None:
        self._tools = tools if tools is not None else [FakeTool("ping", "连通性检查")]
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> FakeToolList:
        return FakeToolList(tools=list(self._tools))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> FakeCallResult:
        self.calls.append((name, arguments))
        result = self._results.get(name)
        if result is None:
            return FakeCallResult(
                structuredContent={"ok": False, "error": f"unknown tool: {name}"},
                isError=True,
                content=[FakeContent(text=f"unknown tool: {name}")],
            )
        return result


def build_rag(tmp_path: Path) -> JwipcKnowledgeRAG:
    """建立只含一条文档的内存知识库。"""
    embedder = FakeEmbedding(dim=DIM)
    config = QdrantConfig(
        mode="local",
        path=":memory:",
        collection_name="service_test",
        vector_size=DIM,
    )
    rag = JwipcKnowledgeRAG(embedder, config)
    document = tmp_path / "qdrant_intro.md"
    document.write_text(DOC_ZH, encoding="utf-8")
    rag.add_document(document)
    return rag


def build_test_tracer(tmp_path: Path) -> Tracer:
    """建立指向 ``tmp_path`` 的本地追踪器。

    **必须显式指定落盘目录**：``ObservabilityConfig`` 的默认值是仓库内的
    ``logs/traces``，不覆盖的话每跑一次测试都会往仓库里写文件，症状是「测试
    全绿但工作区莫名变脏」。后端固定为 ``local``，测试不联网。
    """
    config = ObservabilityConfig(
        enabled=True,
        backend="local",
        trace_dir=tmp_path / "traces",
    )
    return build_tracer(config)


def build_deps(
    tmp_path: Path,
    *,
    rag: JwipcKnowledgeRAG | None = None,
    chat: Callable[[list[dict[str, str]]], Awaitable[str]] | None = None,
    mcp: Any = None,
    settings: AgentServiceSettings | None = None,
) -> AgentServiceDeps:
    """组装一份可直接驱动接口的依赖容器（全部为本地替身，不联网）。"""
    resolved = settings or AgentServiceSettings(session_dir=tmp_path / "sessions")
    deps = AgentServiceDeps.create(resolved, version="0.1.0")
    deps.rag = rag if rag is not None else build_rag(tmp_path)
    deps.chat_fn = chat
    deps.sessions = SessionStore(resolved.session_dir)
    deps.mcp = mcp
    deps.tracer = build_test_tracer(tmp_path)
    deps.graph = build_requirement_workflow(chat_fn=chat, rag=deps.rag)
    deps.set_dependency("session_store", ready=True, required=True, detail="test store")
    deps.set_dependency("config", ready=True, required=True, detail="test config")
    deps.set_dependency("qdrant", ready=True, required=True, detail="in-memory qdrant")
    deps.set_dependency("llm", ready=True, required=True, detail="fake chat")
    deps.set_dependency("workflow", ready=True, required=True, detail="fake chat")
    deps.set_dependency("mcp", ready=mcp is not None, required=False, detail="test mcp")
    deps.set_dependency(
        "observability",
        ready=True,
        required=False,
        detail=f"backend={deps.tracer.backend_name} dir={tmp_path / 'traces'}",
    )
    return deps


@dataclass
class Harness:
    """一组已就绪的测试客户端与配件。"""

    client: httpx.AsyncClient
    deps: AgentServiceDeps
    chat: ServiceChat

    async def post(self, path: str, payload: dict[str, Any], **kwargs: Any) -> httpx.Response:
        return await self.client.post(path, json=payload, **kwargs)

    async def frames(self, path: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """以流式方式请求并解析全部 SSE 数据帧。"""
        frames: list[dict[str, Any]] = []
        async with self.client.stream("POST", path, json=payload) as response:
            assert response.status_code == 200, response.status_code
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[len("data: ") :]))
        return frames
