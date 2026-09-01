"""开发辅助工具集：list_files / read_file / git_log / check_commit_message。

所有文件访问都经由 :class:`~jwipc_dev_mcp_server.security.SandboxRoot`
做路径白名单校验，越界请求一律失败返回，不做任何沙箱外操作。
"""

from __future__ import annotations

import subprocess

from mcp.server.fastmcp import FastMCP

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
)
from .security import SandboxError, SandboxRoot

_GIT_FIELD_SEP = "\x1f"
_GIT_RECORD_SEP = "\x1e"
_LIMIT_CEILING = 200
_GIT_TIMEOUT_SECONDS = 15
_SKIP_DIR_NAMES = {".git"}


def _cap(limit: int | None, default: int) -> int:
    """把调用方传入的条数上限收敛到合法区间。"""
    if not limit:
        return default
    return max(1, min(int(limit), _LIMIT_CEILING))


def register_tools(mcp: FastMCP, config: McpServerConfig) -> None:
    """把四个工具注册到服务实例上；沙箱根目录取自配置。"""
    sandbox = SandboxRoot(config.root)

    @mcp.tool()
    def list_files(path: str = ".", limit: int | None = None) -> ListFilesResult:
        """列出沙箱内某个目录下的文件与子目录（不递归，跳过 .git）。"""
        try:
            target = sandbox.resolve(path)
        except SandboxError as exc:
            return ListFilesResult(ok=False, path=path, error=str(exc), kind=exc.kind)
        if not target.exists():
            return ListFilesResult(
                ok=False, path=path, error=f"目录不存在: {path}", kind="not_found"
            )
        if not target.is_dir():
            return ListFilesResult(ok=False, path=path, error=f"不是目录: {path}", kind="not_dir")

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

    @mcp.tool()
    def read_file(path: str) -> ReadFileResult:
        """读取沙箱内一个 UTF-8 文本文件；超限或二进制文件会被拒绝。"""
        try:
            target = sandbox.resolve(path)
        except SandboxError as exc:
            return ReadFileResult(ok=False, path=path, error=str(exc), kind=exc.kind)
        if not target.exists():
            return ReadFileResult(
                ok=False, path=path, error=f"文件不存在: {path}", kind="not_found"
            )
        if target.is_dir():
            return ReadFileResult(ok=False, path=path, error=f"目标是目录: {path}", kind="is_dir")

        size = target.stat().st_size
        if size > config.max_read_bytes:
            return ReadFileResult(
                ok=False,
                path=path,
                error=f"文件 {size} 字节，超过上限 {config.max_read_bytes} 字节，需人工确认后才能读取",
                kind="too_large",
            )

        raw = target.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ReadFileResult(
                ok=False,
                path=path,
                error="文件不是有效的 UTF-8 文本（疑似二进制文件）",
                kind="binary_or_undecodable",
            )
        return ReadFileResult(ok=True, path=path, content=text, bytes=len(raw))

    @mcp.tool()
    def git_log(path: str = ".", limit: int | None = None) -> GitLogResult:
        """查看沙箱内 git 仓库的最近提交，并逐条校验提交信息格式。"""
        try:
            target = sandbox.resolve(path)
        except SandboxError as exc:
            return GitLogResult(ok=False, path=path, error=str(exc), kind=exc.kind)
        if not target.exists():
            return GitLogResult(ok=False, path=path, error=f"目录不存在: {path}", kind="not_found")
        if not target.is_dir():
            return GitLogResult(ok=False, path=path, error=f"不是目录: {path}", kind="not_dir")

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
            return GitLogResult(ok=False, path=path, error="git 命令超时", kind="git_error")
        except OSError as exc:
            return GitLogResult(ok=False, path=path, error=f"无法执行 git: {exc}", kind="git_error")
        if proc.returncode != 0:
            lines = (proc.stderr or "").strip().splitlines()
            detail = lines[-1] if lines else f"git 退出码 {proc.returncode}"
            return GitLogResult(ok=False, path=path, error=detail, kind="git_error")

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

    @mcp.tool()
    def check_commit_message(message: str) -> CommitCheckResult:
        """校验一条提交信息是否符合 Conventional Commits 规范。"""
        if not message or not message.strip():
            return CommitCheckResult(ok=False, error="提交信息为空", kind="empty_message")
        try:
            verdict = _check_message(message)
        except Exception as exc:  # 校验器自身故障统一降级为失败结果，不向协议层抛异常
            return CommitCheckResult(ok=False, error=f"校验器异常: {exc}", kind="internal")
        return CommitCheckResult(
            ok=True,
            valid=bool(verdict.get("valid")),
            errors=list(verdict.get("errors") or []),
            parsed=CommitParsed(**(verdict.get("parsed") or {})),
        )
