"""MCP 服务的装配入口与命令行启动入口。

服务基于 :class:`mcp.server.fastmcp.FastMCP` 构建。工具通过 :func:`build_server`
注册，这样测试时可以单独构造一个隔离的实例。

日志始终输出到 stderr：在 stdio 传输方式下，stdout 承载的是 JSON-RPC 数据流，
任何写入 stdout 的内容都会破坏协议。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from .config import McpServerConfig
from .tools import register_tools

SERVER_NAME = "jwipc-dev-mcp-server"
"""向客户端上报的服务标识，对应 ``serverInfo.name``。"""


class PingResult(BaseModel):
    """连通性检查工具 :func:`ping` 的返回结果。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    server: str = Field(description="initialize 握手时上报的服务标识。")
    message: str = Field(default="pong", description="固定应答内容。")


def build_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    config: McpServerConfig | None = None,
) -> FastMCP:
    """构建 MCP 服务：ping 连通性检查 + 沙箱开发工具集。

    Args:
        host: Streamable HTTP 传输方式的绑定地址（v1 把 host/port 放在
            构造函数上，而不是 :meth:`FastMCP.run` 里）。
        port: Streamable HTTP 传输方式的绑定端口。
        config: 沙箱与上限配置；缺省用 :class:`McpServerConfig` 的默认值。

    Returns:
        一个配置好的 :class:`FastMCP` 实例；调用 ``run(transport=...)``
        即可启动服务。
    """
    mcp = FastMCP(
        SERVER_NAME,
        host=host,
        port=port,
        streamable_http_path="/mcp",
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Ping",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def ping() -> PingResult:
        """确认服务可达且会话已完成初始化。"""
        return PingResult(server=SERVER_NAME)

    register_tools(mcp, config or McpServerConfig())
    return mcp


def main(argv: Sequence[str] | None = None) -> int:
    """命令行启动入口：以 stdio 或 Streamable HTTP 方式启动服务。"""
    parser = argparse.ArgumentParser(description="启动 jwipc 开发用 MCP 服务。")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="使用的传输方式（默认 stdio）。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 类传输方式的绑定地址。")
    parser.add_argument("--port", type=int, default=8765, help="HTTP 类传输方式的绑定端口。")
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    server = build_server(host=args.host, port=args.port)
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
