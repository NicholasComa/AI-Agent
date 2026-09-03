"""MCP 安全组件测试：路径白名单 / 命令白名单 / 确认门（10 条）。"""

from __future__ import annotations

import pytest

from jwipc_dev_mcp_server.security import (
    CommandPolicyError,
    ConfirmationGate,
    GitCommandPolicy,
    SandboxError,
    SandboxRoot,
)


def test_empty_path_rejected():
    """空路径按写法问题拒绝。"""
    box = SandboxRoot(".")
    with pytest.raises(SandboxError) as exc:
        box.resolve("   ")
    assert exc.value.kind == "bad_path"


def test_absolute_path_rejected():
    """绝对路径按写法问题拒绝。"""
    box = SandboxRoot(".")
    with pytest.raises(SandboxError) as exc:
        box.resolve("/etc/passwd")
    assert exc.value.kind == "bad_path"


def test_drive_path_rejected():
    """Windows 盘符路径按写法问题拒绝。"""
    box = SandboxRoot(".")
    with pytest.raises(SandboxError) as exc:
        box.resolve("C:/Users/x/secret.txt")
    assert exc.value.kind == "bad_path"


def test_traversal_forbidden(mcp_sandbox):
    """.. 穿越被判定为 forbidden。"""
    box = SandboxRoot(mcp_sandbox)
    with pytest.raises(SandboxError) as exc:
        box.resolve("../outside.txt")
    assert exc.value.kind == "forbidden"


def test_symlink_escape_forbidden(tmp_path):
    """符号链接逃逸到沙箱外被判定为 forbidden。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "box"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    if not (root / "link").is_symlink():
        # 受限环境可能静默未创建链接（不抛异常但链接不存在），同样跳过
        pytest.skip("符号链接未真正创建（受限环境）")
    box = SandboxRoot(root)
    with pytest.raises(SandboxError) as exc:
        box.resolve("link/secret.txt")
    assert exc.value.kind == "forbidden"


def test_valid_relative_path_resolves(mcp_sandbox):
    """合法相对路径解析到沙箱内部。"""
    box = SandboxRoot(mcp_sandbox)
    target = box.resolve("sub/inner.md")
    assert target.is_relative_to(box.root)
    assert target.name == "inner.md"


def test_git_policy_rejects_unknown_subcommand():
    """白名单外的 git 子命令被拦截。"""
    policy = GitCommandPolicy()
    with pytest.raises(CommandPolicyError) as exc:
        policy.validate(["git", "checkout", "main"])
    assert exc.value.kind == "argument_rejected"


def test_git_policy_rejects_forbidden_args():
    """白名单外的 git 参数被拦截（含 --all 注入）。"""
    policy = GitCommandPolicy()
    with pytest.raises(CommandPolicyError) as exc:
        policy.validate(["git", "log", "--all"])
    assert exc.value.kind == "argument_rejected"


def test_git_policy_allows_log_argv():
    """git_log 实际使用的 argv 全部放行。"""
    policy = GitCommandPolicy()
    policy.validate(
        [
            "git",
            "-C",
            "/tmp/repo",
            "log",
            "-n20",
            "--date=iso",
            "--pretty=format:%H%x1f%an%x1f%ad%x1f%s%x1e",
        ]
    )  # 不抛异常即通过


def test_confirmation_gate_state_flow():
    """确认门状态流转：未确认→已确认→撤销后回到未确认。"""
    gate = ConfirmationGate()
    key = gate.key("read", "/tmp/big.log")
    assert gate.require(key) == "needs_confirmation"
    gate.confirm(key)
    assert gate.require(key) == "confirmed"
    gate.revoke(key)
    assert gate.require(key) == "needs_confirmation"
