"""把本地开发能力以标准工具的形式暴露出来的 MCP 服务。

对外公开入口：

- :func:`jwipc_dev_mcp_server.server.build_server` —— 构建
  :class:`mcp.server.fastmcp.FastMCP` 实例。
- :func:`jwipc_dev_mcp_server.server.main` —— 命令行入口，用于在
  stdio 与 Streamable HTTP 两种传输方式之间做选择。

本包与传输方式无关：工具就是普通函数，只有在服务真正启动时
才会选定具体的传输方式。
"""

from .server import SERVER_NAME, build_server, main

__all__ = [
    "SERVER_NAME",
    "build_server",
    "main",
]
