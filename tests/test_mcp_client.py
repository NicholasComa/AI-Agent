"""client.py 双传输封装测试：stdio 真实子进程 + Streamable HTTP 全链路。"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from jwipc_dev_mcp_server.client import connect_http, connect_stdio
from jwipc_dev_mcp_server.server import build_server

_SERVER_SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts" / "mcp_week7_server.py")
_REQUIRED_TOOLS = {"ping", "list_files", "read_file", "git_log", "check_commit_message"}
_HTTP_URL = "http://127.0.0.1:8765/mcp"


def _asgi_client(app) -> httpx.AsyncClient:
    """构造带 ASGI transport 的 httpx 客户端（不起真实端口）。"""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app))


@asynccontextmanager
async def _http_session(mcp_config) -> AsyncIterator[ClientSession]:
    """测试辅助：lifespan + Streamable HTTP 客户端 + 已握手会话一次进入。

    ASGITransport 不会触发 Starlette lifespan，而会话管理的 task group
    恰好在 lifespan 里创建；三个上下文按顺序进入，保证握手前置条件齐全。
    每个调用都新建服务实例，避免 session manager 重复 run。
    """
    http_app = build_server(config=mcp_config).streamable_http_app()
    client = _asgi_client(http_app)
    try:
        async with (
            http_app.router.lifespan_context(http_app),
            streamable_http_client(_HTTP_URL, http_client=client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session
    finally:
        await client.aclose()


async def test_connect_stdio_full_chain():
    """stdio 封装：拉起真实子进程，握手、列工具、调 ping 全通。"""
    async with connect_stdio(sys.executable, [_SERVER_SCRIPT, "--transport", "stdio"]) as session:
        assert isinstance(session, ClientSession)
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert names >= _REQUIRED_TOOLS
        res = await session.call_tool("ping", {})
        assert res.structuredContent["message"] == "pong"


async def test_streamable_http_end_to_end(mcp_config):
    """Streamable HTTP 全链路：ASGI transport 上握手、列工具、调工具。"""
    async with _http_session(mcp_config) as session:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert "ping" in names and "git_log" in names
        res = await session.call_tool("list_files", {"path": "."})
        assert res.structuredContent["ok"] is True
        assert res.structuredContent["total"] >= 5


async def test_connect_http_factory(mcp_config):
    """connect_http 工厂：注入 ASGI 客户端后可完成握手并调用工具。"""
    http_app = build_server(config=mcp_config).streamable_http_app()
    client = _asgi_client(http_app)
    try:
        async with (
            http_app.router.lifespan_context(http_app),
            connect_http(_HTTP_URL, http_client=client) as session,
        ):
            res = await session.call_tool("ping", {})
            assert res.structuredContent["message"] == "pong"
    finally:
        await client.aclose()
