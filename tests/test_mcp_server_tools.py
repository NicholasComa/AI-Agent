"""MCP 四个开发工具的常规与边界行为测试（15 条）。"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP

from jwipc_dev_mcp_server.security import ConfirmationGate
from jwipc_dev_mcp_server.tools import register_tools


@pytest.fixture()
def tool_app(mcp_config):
    """只注册四个工具的服务实例，并暴露其确认门。"""
    mcp = FastMCP("test-tools")
    gate = ConfirmationGate()
    register_tools(mcp, mcp_config, gate=gate)
    return mcp, gate


async def _call(app, name: str, arguments: dict) -> dict:
    """调用工具并返回结构化结果；FastMCP.call_tool 返回 ``(content, structured)``。"""
    _, structured = await app.call_tool(name, arguments)
    return dict(structured or {})


async def test_list_files_lists_sandbox_root(tool_app):
    """列根目录：返回常规条目且跳过 .git。"""
    app, _ = tool_app
    data = await _call(app, "list_files", {"path": "."})
    assert data["ok"] is True
    names = {entry["rel_path"] for entry in data["entries"]}
    assert "README.md" in names
    assert "sub" in names
    assert ".git" not in names


async def test_list_files_missing_dir_returns_not_found(tool_app):
    """不存在的目录返回 not_found。"""
    app, _ = tool_app
    data = await _call(app, "list_files", {"path": "no-such-dir"})
    assert data["ok"] is False
    assert data["kind"] == "not_found"


async def test_list_files_file_path_returns_not_dir(tool_app):
    """把文件当目录列出返回 not_dir。"""
    app, _ = tool_app
    data = await _call(app, "list_files", {"path": "README.md"})
    assert data["ok"] is False
    assert data["kind"] == "not_dir"


async def test_read_file_reads_text(tool_app):
    """正常文本文件可读。"""
    app, _ = tool_app
    data = await _call(app, "read_file", {"path": "README.md"})
    assert data["ok"] is True
    assert "Sandbox" in data["content"]
    assert data["bytes"] > 0


async def test_read_file_missing_returns_not_found(tool_app):
    """不存在的文件返回 not_found。"""
    app, _ = tool_app
    data = await _call(app, "read_file", {"path": "no-such.txt"})
    assert data["ok"] is False
    assert data["kind"] == "not_found"


async def test_read_file_directory_returns_is_dir(tool_app):
    """把目录当文件读取返回 is_dir。"""
    app, _ = tool_app
    data = await _call(app, "read_file", {"path": "sub"})
    assert data["ok"] is False
    assert data["kind"] == "is_dir"


async def test_read_file_large_needs_confirmation(tool_app):
    """超限大文件在未确认时返回 needs_confirmation。"""
    app, _ = tool_app
    data = await _call(app, "read_file", {"path": "large_log.txt"})
    assert data["ok"] is False
    assert data["kind"] == "needs_confirmation"


async def test_read_file_large_after_confirm(tool_app, mcp_config):
    """确认门放行后，同一大文件可以正常读取。"""
    app, gate = tool_app
    target = (mcp_config.root / "large_log.txt").resolve()
    gate.confirm(gate.key("read", str(target)))
    data = await _call(app, "read_file", {"path": "large_log.txt"})
    assert data["ok"] is True
    assert data["bytes"] == mcp_config.max_read_bytes + 10_000


async def test_read_file_binary_returns_undecodable(tool_app):
    """二进制文件返回 binary_or_undecodable。"""
    app, _ = tool_app
    data = await _call(app, "read_file", {"path": "binary.dat"})
    assert data["ok"] is False
    assert data["kind"] == "binary_or_undecodable"


async def test_git_log_returns_two_commits(tool_app):
    """沙箱 git 仓库能取回两条提交。"""
    app, _ = tool_app
    data = await _call(app, "git_log", {"path": "repo"})
    assert data["ok"] is True
    assert data["returned"] == 2
    assert data["truncated"] is False


async def test_git_log_marks_message_validity(tool_app):
    """提交信息逐条校验：合格式为 True，不合格式为 False（最新在前）。"""
    app, _ = tool_app
    data = await _call(app, "git_log", {"path": "repo"})
    commits = data["commits"]
    assert commits[0]["message_valid"] is True  # func: app: add sandbox sample files
    assert commits[1]["message_valid"] is False  # update files


async def test_git_log_non_repo_returns_git_error(tool_app):
    """非 git 仓库目录返回 git_error。"""
    app, _ = tool_app
    data = await _call(app, "git_log", {"path": "sub"})
    assert data["ok"] is False
    assert data["kind"] == "git_error"


async def test_check_commit_message_valid(tool_app):
    """合格式提交信息判定为 valid。"""
    app, _ = tool_app
    data = await _call(
        app, "check_commit_message", {"message": "func: app: add sandbox sample files"}
    )
    assert data["ok"] is True
    assert data["valid"] is True
    assert data["errors"] == []


async def test_check_commit_message_invalid(tool_app):
    """不合格式提交信息判定为 invalid 并给出原因。"""
    app, _ = tool_app
    data = await _call(app, "check_commit_message", {"message": "update files"})
    assert data["ok"] is True
    assert data["valid"] is False
    assert len(data["errors"]) > 0


async def test_check_commit_message_empty(tool_app):
    """空提交信息返回 empty_message 失败信封。"""
    app, _ = tool_app
    data = await _call(app, "check_commit_message", {"message": ""})
    assert data["ok"] is False
    assert data["kind"] == "empty_message"
