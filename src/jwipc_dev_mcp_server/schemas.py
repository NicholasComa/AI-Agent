"""MCP 工具的出参数据模型；SDK 依据这些模型自动生成 outputSchema。

所有结果模型继承 :class:`ToolResult` 公共信封（``ok``/``error``/``kind`` 平铺），
失败结果统一用 :func:`tool_failure` 构造，保证各工具的错误返回形态一致。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


def _forbid() -> ConfigDict:
    """统一禁许多余字段：多传即报错，不静默忽略。"""
    return ConfigDict(extra="forbid")


class ToolResult(BaseModel):
    """工具返回结果的公共信封：成功/失败字段对所有工具一致。"""

    model_config = _forbid()

    ok: bool = True
    error: str | None = Field(default=None, description="失败原因，成功时为空")
    kind: str | None = Field(default=None, description="错误类别，如 forbidden/not_found/too_large")


def tool_failure[T: ToolResult](
    result_type: type[T], *, error: str, kind: str, **fields: object
) -> T:
    """构造统一的失败结果：``ok=False`` 并平铺错误字段。"""
    return result_type(ok=False, error=error, kind=kind, **fields)


class FileEntry(BaseModel):
    """单条文件/目录条目。"""

    model_config = _forbid()

    rel_path: str
    is_dir: bool
    size_bytes: int = 0


class ListFilesResult(ToolResult):
    """list_files 的返回结果。"""

    path: str = "."
    entries: list[FileEntry] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False


class ReadFileResult(ToolResult):
    """read_file 的返回结果。"""

    path: str = ""
    content: str = ""
    bytes: int = 0
    truncated: bool = False


class CommitLogEntry(BaseModel):
    """单条提交记录。"""

    model_config = _forbid()

    sha: str
    author: str = ""
    date: str = ""
    subject: str = ""
    message_valid: bool = False


class GitLogResult(ToolResult):
    """git_log 的返回结果。"""

    path: str = "."
    commits: list[CommitLogEntry] = Field(default_factory=list)
    returned: int = 0
    truncated: bool = False


class CommitParsed(BaseModel):
    """提交信息解析结果；解析失败时字段允许为空。"""

    model_config = _forbid()

    type: str | None = None
    scope: str | None = None
    subject: str | None = None
    breaking: bool | None = None
    has_body: bool | None = None


class CommitCheckResult(ToolResult):
    """check_commit_message 的返回结果。"""

    valid: bool = False
    errors: list[str] = Field(default_factory=list)
    parsed: CommitParsed = Field(default_factory=CommitParsed)
