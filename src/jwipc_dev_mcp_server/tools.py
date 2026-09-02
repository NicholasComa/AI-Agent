"""开发辅助工具集：list_files / read_file / git_log / check_commit_message。

所有文件访问都经由 :class:`~jwipc_dev_mcp_server.security.SandboxRoot`
做路径白名单校验；git 命令再经 :class:`~jwipc_dev_mcp_server.security.GitCommandPolicy`
做子命令与参数白名单校验；超限文件读取需经
:class:`~jwipc_dev_mcp_server.security.ConfirmationGate` 人工确认。
失败结果统一用 :func:`~jwipc_dev_mcp_server.schemas.tool_failure` 构造。
"""

from __future__ import annotations

import subprocess

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from devagent.tools.check_commit_message import check_commit_message as _check_message

from .config import McpServerConfig
from .schemas import (
    CommitCheckResult,
    CommitLogEntry,
    CommitParsed,
    FileEntry,
    GitLogResult,
    ListFilesResult,
    ReadFileResult,
    tool_failure,
)
from .security import (
    CommandPolicyError,
    ConfirmationGate,
    GitCommandPolicy,
    SandboxError,
    SandboxRoot,
)

_GIT_FIELD_SEP = "\x1f"
_GIT_RECORD_SEP = "\x1e"
_LIMIT_CEILING = 200
_GIT_TIMEOUT_SECONDS = 15
_SKIP_DIR_NAMES = {".git"}

_READ_ONLY_HINTS: dict[str, bool] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
"""四个工具共用的只读行为标签（自我声明，不替代沙箱等硬边界）。"""


def _cap(limit: int | None, default: int) -> int:
    """把调用方传入的条数上限收敛到合法区间。"""
    if not limit:
        return default
    return max(1, min(int(limit), _LIMIT_CEILING))


def register_tools(
    mcp: FastMCP,
    config: McpServerConfig,
    gate: ConfirmationGate | None = None,
) -> ConfirmationGate:
    """把四个工具注册到服务实例上，返回使用的确认门。

    Args:
        mcp: 目标 FastMCP 实例。
        config: 沙箱根目录与各类上限。
        gate: 外部注入的确认门；缺省内部新建，便于测试预置确认状态。
    """
    sandbox = SandboxRoot(config.root)
    gate = gate or ConfirmationGate()
    git_policy = GitCommandPolicy()

    @mcp.tool(
        name="list_files",
        annotations=ToolAnnotations(title="List Files", **_READ_ONLY_HINTS),
    )
    def list_files(path: str = ".", limit: int | None = None) -> ListFilesResult:
        """列出沙箱内某个目录下的文件与子目录（不递归，跳过 .git）。"""
        try:
            target = sandbox.resolve(path)
        except SandboxError as exc:
            return tool_failure(ListFilesResult, path=path, error=str(exc), kind=exc.kind)
        if not target.exists():
            return tool_failure(
                ListFilesResult, path=path, error=f"目录不存在: {path}", kind="not_found"
            )
        if not target.is_dir():
            return tool_failure(
                ListFilesResult, path=path, error=f"不是目录: {path}", kind="not_dir"
            )

        rows: list[FileEntry] = []
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if item.name in _SKIP_DIR_NAMES:
                continue
            is_dir = item.is_dir()
            rows.append(
                FileEntry(
                    rel_path=item.relative_to(sandbox.root).as_posix(),
                    is_dir=is_dir,
                    size_bytes=0 if is_dir else item.stat().st_size,
                )
            )
        cap = _cap(limit, config.list_limit)
        truncated = len(rows) > cap
        return ListFilesResult(
            ok=True,
            path=path,
            entries=rows[:cap],
            total=len(rows),
            truncated=truncated,
        )

    @mcp.tool(
        name="read_file",
        annotations=ToolAnnotations(title="Read File", **_READ_ONLY_HINTS),
    )
    def read_file(path: str) -> ReadFileResult:
        """读取沙箱内一个 UTF-8 文本文件；超限文件需确认，二进制会被拒绝。"""
        try:
            target = sandbox.resolve(path)
        except SandboxError as exc:
            return tool_failure(ReadFileResult, path=path, error=str(exc), kind=exc.kind)
        if not target.exists():
            return tool_failure(
                ReadFileResult, path=path, error=f"文件不存在: {path}", kind="not_found"
            )
        if target.is_dir():
            return tool_failure(
                ReadFileResult, path=path, error=f"目标是目录: {path}", kind="is_dir"
            )

        size = target.stat().st_size
        if size > config.max_read_bytes:
            confirm_key = gate.key("read", str(target))
            if gate.require(confirm_key) != "confirmed":
                return tool_failure(
                    ReadFileResult,
                    path=path,
                    error=f"文件 {size} 字节，超过上限 {config.max_read_bytes} 字节，确认后才能读取",
                    kind="needs_confirmation",
                )

        raw = target.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return tool_failure(
                ReadFileResult,
                path=path,
                error="文件不是有效的 UTF-8 文本（疑似二进制文件）",
                kind="binary_or_undecodable",
            )
        return ReadFileResult(ok=True, path=path, content=text, bytes=len(raw))

    @mcp.tool(
        name="git_log",
        annotations=ToolAnnotations(title="Git Log", **_READ_ONLY_HINTS),
    )
    def git_log(path: str = ".", limit: int | None = None) -> GitLogResult:
        """查看沙箱内 git 仓库的最近提交，并逐条校验提交信息格式。"""
        try:
            target = sandbox.resolve(path)
        except SandboxError as exc:
            return tool_failure(GitLogResult, path=path, error=str(exc), kind=exc.kind)
        if not target.exists():
            return tool_failure(
                GitLogResult, path=path, error=f"目录不存在: {path}", kind="not_found"
            )
        if not target.is_dir():
            return tool_failure(GitLogResult, path=path, error=f"不是目录: {path}", kind="not_dir")

        cap = _cap(limit, config.git_log_limit)
        # 分隔符用 git 的 %x 转义（纯 ASCII），避免 Windows 传参吞掉字面控制字符
        argv = [
            "git",
            "-C",
            str(target),
            "log",
            f"-n{cap}",
            "--date=iso",
            "--pretty=format:%H%x1f%an%x1f%ad%x1f%s%x1e",
        ]
        try:
            git_policy.validate(argv)
        except CommandPolicyError as exc:
            return tool_failure(GitLogResult, path=path, error=str(exc), kind=exc.kind)
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return tool_failure(GitLogResult, path=path, error="git 命令超时", kind="git_error")
        except OSError as exc:
            return tool_failure(
                GitLogResult, path=path, error=f"无法执行 git: {exc}", kind="git_error"
            )
        if proc.returncode != 0:
            lines = (proc.stderr or "").strip().splitlines()
            detail = lines[-1] if lines else f"git 退出码 {proc.returncode}"
            return tool_failure(GitLogResult, path=path, error=detail, kind="git_error")

        commits: list[CommitLogEntry] = []
        for record in proc.stdout.split(_GIT_RECORD_SEP):
            fields = record.strip("\n").split(_GIT_FIELD_SEP)
            if len(fields) != 4 or not fields[0]:
                continue
            sha, author, date, subject = (field.strip() for field in fields)
            commits.append(
                CommitLogEntry(
                    sha=sha,
                    author=author,
                    date=date,
                    subject=subject,
                    message_valid=bool(_check_message(subject).get("valid")),
                )
            )
        return GitLogResult(
            ok=True,
            path=path,
            commits=commits,
            returned=len(commits),
            truncated=len(commits) >= cap,
        )

    @mcp.tool(
        name="check_commit_message",
        annotations=ToolAnnotations(title="Check Commit Message", **_READ_ONLY_HINTS),
    )
    def check_commit_message(message: str) -> CommitCheckResult:
        """校验一条提交信息是否符合 Conventional Commits 规范。"""
        if not message or not message.strip():
            return tool_failure(CommitCheckResult, error="提交信息为空", kind="empty_message")
        try:
            verdict = _check_message(message)
        except Exception as exc:  # 校验器自身故障统一降级为失败结果，不向协议层抛异常
            return tool_failure(CommitCheckResult, error=f"校验器异常: {exc}", kind="internal")
        return CommitCheckResult(
            ok=True,
            valid=bool(verdict.get("valid")),
            errors=list(verdict.get("errors") or []),
            parsed=CommitParsed(**(verdict.get("parsed") or {})),
        )

    return gate
