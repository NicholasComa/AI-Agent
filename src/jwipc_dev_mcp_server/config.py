"""MCP 服务的运行配置：沙箱根目录与各类访问上限。"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

DEFAULT_MAX_READ_BYTES = 50_000  #单次读取文件的最大字节数
DEFAULT_LIST_LIMIT = 200  #默认返回的最大条目数
DEFAULT_GIT_LOG_LIMIT = 20  #默认返回的最大提交数

_ENV_PREFIX = "JWIPC_MCP_"
_DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "data" / "mcp_sandbox"


class McpServerConfig:
    """MCP 服务运行参数。

    Attributes:
        root: 沙箱根目录，文件类工具只允许访问该目录内部。
        max_read_bytes: 单次读取文件的最大字节数，超过即拒绝（读取闸门）。
        list_limit: list_files 默认返回的条目上限。
        git_log_limit: git_log 默认返回的提交条数上限。
    """

    def __init__(
        self,
        *,
        root: Path | str | None = None,
        max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
        list_limit: int = DEFAULT_LIST_LIMIT,
        git_log_limit: int = DEFAULT_GIT_LOG_LIMIT,
    ) -> None:
        self.root = Path(root).resolve() if root is not None else _DEFAULT_ROOT.resolve()
        self.max_read_bytes = max_read_bytes
        self.list_limit = list_limit
        self.git_log_limit = git_log_limit

    @classmethod
    def from_env(cls, source: dict[str, str] | None = None) -> McpServerConfig:
        """从环境变量构建配置，变量名形如 ``JWIPC_MCP_ROOT``。

        非法取值直接忽略并回落默认值，保证服务总能启动。
        """
        env = os.environ if source is None else source

        def take(name: str, cast: type) -> object | None:
            value = env.get(_ENV_PREFIX + name)
            if not value:
                return None
            with suppress(ValueError, TypeError):
                return cast(value)
            return None

        return cls(
            root=take("ROOT", Path),
            max_read_bytes=take("MAX_READ_BYTES", int) or DEFAULT_MAX_READ_BYTES,
            list_limit=take("LIST_LIMIT", int) or DEFAULT_LIST_LIMIT,
            git_log_limit=take("GIT_LOG_LIMIT", int) or DEFAULT_GIT_LOG_LIMIT,
        )
