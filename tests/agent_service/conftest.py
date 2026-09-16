"""``src/agent_service`` 测试夹具。

只放 fixture；替身与工具函数在 :mod:`service_fakes`，测试文件直接
``from service_fakes import ...`` 复用。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from service_fakes import QUERY_HIT, Harness, ServiceChat, build_deps, build_rag

from agent_service import create_agent_service_app


@pytest.fixture
def chunk_id(tmp_path: Path) -> str:
    """语料中被允许引用的片段 ID。"""
    rag = build_rag(tmp_path)
    hits = rag.retrieve(QUERY_HIT, top_k=1)
    assert hits, "夹具语料应能召回结果"
    return hits[0].chunk_id


@pytest.fixture
async def harness(tmp_path: Path, chunk_id: str) -> AsyncIterator[Harness]:
    """默认夹具：内存知识库 + 假模型 + 未启用的 MCP。"""
    chat = ServiceChat(chunk_id)
    deps = build_deps(tmp_path, chat=chat)
    app = create_agent_service_app(deps=deps, version="0.1.0")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        yield Harness(client=client, deps=deps, chat=chat)
