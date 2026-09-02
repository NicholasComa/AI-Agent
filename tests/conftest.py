"""全局 pytest fixture：MCP 测试共享的沙箱、配置与服务实例。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jwipc_dev_mcp_server.config import McpServerConfig
from jwipc_dev_mcp_server.server import build_server

MAX_READ_BYTES = 50_000


def _write(root: Path, rel: str, content: str | bytes) -> None:
    """在沙箱内写一个文件（自动建父目录）。"""
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


def _make_git_repo(repo_dir: Path) -> None:
    """在指定目录建 git 仓库，含两条样本提交（repo 级身份，不依赖全局配置）。"""
    repo_dir.mkdir(parents=True, exist_ok=True)
    # 初始化仓库、配置身份、配置邮箱、提交一个文件、提交一个空提交
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "sandbox-demo"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "sandbox@example.com"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    _write(repo_dir, "tracked.txt", "tracked by first commit\n")
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "update files"], cwd=repo_dir, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "func: app: add sandbox sample files"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def mcp_sandbox(tmp_path_factory) -> Path:
    """一个临时沙箱：常规文件 + 超限大文件 + 二进制文件 + 独立 git 仓库（repo/）。

    根目录本身不是 git 仓库，git 仓库放在 ``repo/`` 子目录，这样
    根目录下其他目录（如 ``sub/``）可用来测「非仓库」场景。
    """
    root = tmp_path_factory.mktemp("mcp_sandbox")
    _write(root, "README.md", "# Sandbox\nsample text\n")
    _write(root, "notes.txt", "note line\n")
    _write(root, "sub/inner.md", "inner\n")
    _write(root, "sub/deep/deep.txt", "deep\n")
    _write(root, "large_log.txt", "x" * (MAX_READ_BYTES + 10_000))
    _write(root, "binary.dat", b"\xde\xad\xbe\xef" * 100)
    _make_git_repo(root / "repo")
    return root


@pytest.fixture(scope="session")
def mcp_config(mcp_sandbox) -> McpServerConfig:
    """指向临时沙箱的运行配置。"""
    return McpServerConfig(root=mcp_sandbox, max_read_bytes=MAX_READ_BYTES)


@pytest.fixture(scope="session")
def mcp_app(mcp_config):
    """完整服务实例（ping + 四个工具），供端到端测试使用。"""
    return build_server(config=mcp_config)
