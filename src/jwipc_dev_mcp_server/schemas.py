"""MCP 工具的出参数据模型；SDK 依据这些模型自动生成 outputSchema。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


def _forbid() -> ConfigDict:
    """统一禁许多余字段：多传即报错，不静默忽略。"""
    return ConfigDict(extra="forbid")


class FileEntry(BaseModel):
    """单条文件/目录条目。"""

    model_config = _forbid()

    rel_path: str
    is_dir: bool
    size_bytes: int = 0


class ListFilesResult(BaseModel):
    """list_files 的返回结果。"""

    model_config = _forbid()

    ok: bool = True
    path: str = "."
    entries: list[FileEntry] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False
    error: str | None = Field(default=None, description="失败原因，成功时为空")
    kind: str | None = Field(
        default=None, description="错误类别: not_found/not_dir/bad_path/forbidden"
    )


class ReadFileResult(BaseModel):
    """read_file 的返回结果。"""

    model_config = _forbid()

    ok: bool = True
    path: str = ""
    content: str = ""
    bytes: int = 0
    truncated: bool = False
    error: str | None = Field(default=None, description="失败原因，成功时为空")
    kind: str | None = Field(
        default=None,
        description="错误类别: not_found/is_dir/too_large/binary_or_undecodable/bad_path/forbidden",
    )


class CommitLogEntry(BaseModel):
    """单条提交记录。"""

    model_config = _forbid()

    sha: str
    author: str = ""
    date: str = ""
    subject: str = ""
    message_valid: bool = False


class GitLogResult(BaseModel):
    """git_log 的返回结果。"""

    model_config = _forbid()

    ok: bool = True
    path: str = "."
    commits: list[CommitLogEntry] = Field(default_factory=list)
    returned: int = 0
    truncated: bool = False
    error: str | None = Field(default=None, description="失败原因，成功时为空")
    kind: str | None = Field(
        default=None, description="错误类别: not_found/not_dir/git_error/bad_path/forbidden"
    )


class CommitParsed(BaseModel):
    """提交信息解析结果；解析失败时字段允许为空。"""

    model_config = _forbid()

    type: str | None = None
    scope: str | None = None
    subject: str | None = None
    breaking: bool | None = None
    has_body: bool | None = None


class CommitCheckResult(BaseModel):
    """check_commit_message 的返回结果。"""

    model_config = _forbid()

    ok: bool = True
    valid: bool = False
    errors: list[str] = Field(default_factory=list)
    parsed: CommitParsed = Field(default_factory=CommitParsed)
    error: str | None = Field(default=None, description="工具自身故障时填写，校验失败不算故障")
    kind: str | None = Field(default=None, description="错误类别: empty_message/internal")
