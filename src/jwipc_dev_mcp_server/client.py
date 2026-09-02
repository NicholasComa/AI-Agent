"""MCP 客户端封装：stdio 与 Streamable HTTP 两种传输的统一接入。

两个工厂都是异步上下文管理器，进入时完成握手（v1 的
``initialize()`` 必须显式调用），退出时自动关闭会话与通道。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


@asynccontextmanager
async def connect_stdio(
    command: str,
    args: list[str],
    *,
    env: Mapping[str, str] | None = None,
) -> AsyncIterator[ClientSession]:
    """以 stdio 方式连接一个 Server：把命令拉起为子进程并完成握手。

    Args:
        command: 启动 Server 的可执行文件。
        args: 传给 Server 的命令行参数。
        env: 追加给子进程的环境变量，缺省继承当前进程环境。
    """
    params = StdioServerParameters(
        command=command,
        args=list(args),
        env=dict(env) if env is not None else None,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


@asynccontextmanager
async def connect_http(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> AsyncIterator[ClientSession]:
    """以 Streamable HTTP 方式连接一个 Server 并完成握手。

    Args:
        url: Server 的 HTTP 端点（默认 ``/mcp``）。
        headers: 附加的请求头，如鉴权头。
        http_client: 外部注入的 ``httpx.AsyncClient``；测试可传带
            ``ASGITransport`` 的客户端，在不起真实端口的情况下跑通全链路。
            缺省由本函数按真实 HTTP 自建并负责关闭。
    """
    owned = http_client is None
    client = http_client if http_client is not None else httpx.AsyncClient(headers=headers)
    try:
        async with (
            streamable_http_client(url, http_client=client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session
    finally:
        if owned:
            await client.aclose()
