"""「审查一次提交改动」业务流水线：把 4 个 MCP 工具串成一条审查链路。

流水线步骤（客户端视角，通过 MCP 协议编排 Server 的工具）：

    git_log(仓库)            # 拉提交列表，逐条带 message_valid 预判
      → check_commit_message # 对不合格式提交做深入校验（errors/parsed 详情）
      → list_files(仓库)     # 列出仓库内文件与子目录
      → read_file(README.md) # 读关键文件做内容预览
      → 汇总输出结构化审查结果（JSON）

用法（Git Bash，先 ``export PATH="/c/Users/Xsz/.local/bin:$PATH"``）：

    # stdio 模式：自动拉起 Server 子进程（默认）
    uv run python scripts/mcp_week7_review.py

    # Streamable HTTP 模式：先另开终端启动 Server，再连它
    uv run python scripts/mcp_week7_server.py --transport streamable-http
    uv run python scripts/mcp_week7_review.py --transport streamable-http

    # 演示 3 个异常场景（越界访问 / 大文件未确认 / git 参数注入）
    uv run python scripts/mcp_week7_review.py --demo-errors

退出码 0 表示审查完成，否则 1。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # noqa: E402

from jwipc_dev_mcp_server.client import connect_http, connect_stdio  # noqa: E402
from jwipc_dev_mcp_server.security import CommandPolicyError, GitCommandPolicy  # noqa: E402

_SERVER_SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts" / "mcp_week7_server.py")
_DEFAULT_HTTP_URL = "http://127.0.0.1:8765/mcp"
_PREVIEW_LINES = 5


async def _call(session: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """调用一个工具并返回 structuredContent 字典。"""
    result = await session.call_tool(name, args)
    return result.structuredContent or {}


def _preview(text: str, lines: int = _PREVIEW_LINES) -> str:
    """取文本前 N 行作为内容预览。"""
    return "\n".join(text.splitlines()[:lines])


async def run_review(session: Any, repo: str) -> dict[str, Any]:
    """执行「审查一次提交改动」流水线，返回结构化审查结果。"""
    report: dict[str, Any] = {"repo": repo, "steps": []}

    # 第 1 步：拉提交列表（每条已带 message_valid 预判）
    log = await _call(session, "git_log", {"path": repo})
    if not log.get("ok"):
        report["error"] = f"git_log 失败: {log.get('error')}"
        return report
    commits = log.get("commits", [])
    valid = [c for c in commits if c.get("message_valid")]
    invalid = [c for c in commits if not c.get("message_valid")]
    report["steps"].append(
        {
            "step": "git_log",
            "detail": f"共 {len(commits)} 条提交，合格式 {len(valid)} 条，不合格式 {len(invalid)} 条",
        }
    )

    # 第 2 步：对不合格式提交做深入校验，拿到具体违规原因
    invalid_details = []
    for commit in invalid:
        check = await _call(session, "check_commit_message", {"message": commit.get("subject", "")})
        invalid_details.append(
            {
                "sha": commit.get("sha"),
                "subject": commit.get("subject"),
                "valid": check.get("valid"),
                "errors": check.get("errors", []),
                "parsed": check.get("parsed"),
            }
        )
    report["steps"].append(
        {
            "step": "check_commit_message",
            "detail": f"对 {len(invalid_details)} 条不合格式提交完成深入校验",
        }
    )

    # 第 3 步：列出仓库文件（不递归，跳过 .git）
    files = await _call(session, "list_files", {"path": repo})
    entries = files.get("entries", []) if files.get("ok") else []
    report["steps"].append(
        {"step": "list_files", "detail": f"仓库顶层共 {len(entries)} 项（.git 已跳过）"}
    )

    # 第 4 步：读取 README 做内容预览（仓库自述即「改动说明」样本）
    readme = await _call(session, "read_file", {"path": "README.md"})
    preview = _preview(readme.get("content", "")) if readme.get("ok") else readme.get("error", "")
    report["steps"].append(
        {
            "step": "read_file",
            "detail": "README.md 读取成功"
            if readme.get("ok")
            else f"读取失败: {readme.get('error')}",
        }
    )

    # 汇总结构化审查结论
    report["review"] = {
        "files": [
            {
                "rel_path": e.get("rel_path"),
                "is_dir": e.get("is_dir"),
                "size_bytes": e.get("size_bytes"),
            }
            for e in entries
        ],
        "readme_preview": preview,
        "commits_total": len(commits),
        "commits_valid": len(valid),
        "commits_invalid": len(invalid),
        "invalid_details": invalid_details,
        "conclusion": (
            f"审查完成：{len(commits)} 条提交中 {len(valid)} 条符合 type: scope: subject 规范，"
            f"{len(invalid)} 条不合格式（明细见 invalid_details）。"
            if commits
            else "审查完成：仓库没有任何提交。"
        ),
    }
    return report


async def demo_errors(session: Any) -> None:
    """演示 3 个异常场景的协议返回：越界 / 大文件未确认 / git 参数注入。"""
    print("== 异常场景 1：越界访问（.. 穿越） ==")
    data = await _call(session, "list_files", {"path": "../.."})
    print(json.dumps(data, ensure_ascii=False, indent=2))

    print("\n== 异常场景 2：大文件未确认（超过 50KB 上限） ==")
    data = await _call(session, "read_file", {"path": "large_log.txt"})
    print(json.dumps(data, ensure_ascii=False, indent=2))

    print("\n== 异常场景 3：git 参数注入（本地策略演示） ==")
    print("说明：git_log 的命令行由服务端拼装，客户端只能传 path/limit，")
    print("      协议层无法注入 git 参数；此处直接演示 GitCommandPolicy 的拦截行为。")
    for argv in (
        ["git", "-C", "data/mcp_sandbox", "log", "-n5"],
        ["git", "push", "--force", "origin", "main"],
        ["git", "log", "--all", "--grep=password"],
    ):
        try:
            GitCommandPolicy().validate(argv)
            print(f"  放行: {argv}")
        except CommandPolicyError as exc:
            print(f"  拦截: {argv}\n        kind={exc.kind}  {exc.message}")


async def _main() -> int:
    parser = argparse.ArgumentParser(description="「审查一次提交改动」MCP 业务流水线。")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="连接 Server 的传输方式（默认 stdio，自动拉起子进程）。",
    )
    parser.add_argument(
        "--url",
        default=_DEFAULT_HTTP_URL,
        help="streamable-http 模式的 Server 端点地址。",
    )
    parser.add_argument(
        "--repo", default=".", help="沙箱内待审查的 git 仓库相对路径（默认仓库根）。"
    )
    parser.add_argument(
        "--demo-errors",
        action="store_true",
        help="只跑 3 个异常场景演示，不执行正常审查流水线。",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        async with connect_stdio(
            sys.executable, [_SERVER_SCRIPT, "--transport", "stdio"]
        ) as session:
            report = None if args.demo_errors else await run_review(session, args.repo)
            if args.demo_errors:
                await demo_errors(session)
    else:
        async with connect_http(args.url) as session:
            report = None if args.demo_errors else await run_review(session, args.repo)
            if args.demo_errors:
                await demo_errors(session)

    if report is not None:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
