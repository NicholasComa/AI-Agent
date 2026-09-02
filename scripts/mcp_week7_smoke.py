"""MCP 服务冒烟脚本：stdio 或 Streamable HTTP 全链路快速验证。

用法（Git Bash，先 ``export PATH="/c/Users/Xsz/.local/bin:$PATH"``）：

    # stdio 模式：自动拉起 Server 子进程
    uv run python scripts/mcp_week7_smoke.py --transport stdio

    # Streamable HTTP 模式：先另开终端启动 Server，再连它
    uv run python scripts/mcp_week7_server.py --transport streamable-http --port 8765
    uv run python scripts/mcp_week7_smoke.py --transport streamable-http

退出码 0 表示全部检查通过，否则 1。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # noqa: E402

from jwipc_dev_mcp_server.client import connect_http, connect_stdio  # noqa: E402

_SERVER_SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts" / "mcp_week7_server.py")
_DEFAULT_HTTP_URL = "http://127.0.0.1:8765/mcp"
_REQUIRED_TOOLS = {"ping", "list_files", "read_file", "git_log", "check_commit_message"}


def _ok(label: str, detail: str = "") -> None:
    print(f"[PASS] {label}" + (f"  {detail}" if detail else ""))


def _fail(label: str, detail: str) -> None:
    print(f"[FAIL] {label}  {detail}")


async def _check_session(session) -> int:
    """对已握手会话跑一遍冒烟检查，返回未通过项数。"""
    failed = 0

    tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    missing = _REQUIRED_TOOLS - names
    if missing:
        _fail("list_tools", f"缺少工具: {sorted(missing)}")
        failed += 1
    else:
        _ok("list_tools", f"{len(names)} 个工具就绪")

    res = await session.call_tool("ping", {})
    if res.structuredContent.get("message") == "pong":
        _ok("ping")
    else:
        _fail("ping", str(res.structuredContent))
        failed += 1

    res = await session.call_tool("list_files", {"path": "."})
    data = res.structuredContent or {}
    if data.get("ok"):
        _ok("list_files", f"共 {data.get('total')} 项")
    else:
        _fail("list_files", str(data))
        failed += 1

    res = await session.call_tool("read_file", {"path": "README.md"})
    data = res.structuredContent or {}
    if data.get("ok"):
        _ok("read_file", "README.md 可读")
    else:
        _fail("read_file", str(data))
        failed += 1

    res = await session.call_tool(
        "check_commit_message", {"message": "func: app: add sandbox sample files"}
    )
    data = res.structuredContent or {}
    if data.get("valid") is True:
        _ok("check_commit_message", "合法提交信息判定正确")
    else:
        _fail("check_commit_message", str(data))
        failed += 1

    res = await session.call_tool("git_log", {"path": "."})
    data = res.structuredContent or {}
    if data.get("ok") and data.get("returned", 0) >= 1:
        _ok("git_log", f"{data.get('returned')} 条提交")
    else:
        _fail("git_log", str(data))
        failed += 1

    print(f"\n{'全部通过' if failed == 0 else f'共 {failed} 项未通过'}")
    return 0 if failed == 0 else 1


async def _main() -> int:
    parser = argparse.ArgumentParser(description="MCP 服务冒烟验证。")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="待验证的传输方式（默认 stdio）。",
    )
    parser.add_argument(
        "--url",
        default=_DEFAULT_HTTP_URL,
        help="streamable-http 模式的 Server 端点地址。",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        async with connect_stdio(
            sys.executable, [_SERVER_SCRIPT, "--transport", "stdio"]
        ) as session:
            return await _check_session(session)
    async with connect_http(args.url) as session:
        return await _check_session(session)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
