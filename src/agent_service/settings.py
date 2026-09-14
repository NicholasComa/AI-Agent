"""Agent 服务的运行配置。

配置项统一使用 ``AGENT_SERVICE_`` 前缀，大小写不敏感：
``AGENT_SERVICE_MAX_CONCURRENCY`` 与 ``agent_service_max_concurrency`` 等价。
取值来源按优先级为「进程环境变量 > 项目根 ``.env`` > 字段默认值」。

基础设施连接信息（模型接口、Embedding、Qdrant 连接）不在这里重复声明，
沿用项目既有配置源（``.env`` 的 ``API_BASE_URL`` / ``MODEL_NAME`` /
``QDRANT_*`` 等），由 :mod:`agent_service.lifespan` 组装时读取。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "AGENT_SERVICE_"
"""环境变量前缀。"""

DEFAULT_SESSION_DIR = Path("data/agent_service_sessions")
"""会话记录与幂等缓存的缺省落盘目录。"""

DEFAULT_COLLECTION = "jwipc_v3"
"""Qdrant 知识库集合名缺省值，与既有语料导入脚本一致。"""

DEFAULT_MCP_SERVER_SCRIPT = Path("scripts/mcp_week7_server.py")
"""stdio 传输下要拉起的 MCP Server 脚本。"""


class AgentServiceSettings(BaseSettings):
    """服务级可调参数，全部有合理默认值，缺省配置即可启动。

    Attributes:
        api_key: Bearer 认证密钥。空字符串表示不启用认证。
        max_concurrency: 重路由的服务级并发上限，超过后在闸门处排队。
        queue_timeout_seconds: 在并发闸门处的最长排队时间，超时返回限流错误。
        rate_limit_per_minute: 单客户端每分钟请求配额。
        max_body_bytes: 请求体字节上限。
        request_timeout_seconds: 单次请求的处理超时，含上游调用。
        session_dir: 会话记录与幂等缓存目录；容器内挂 Volume。
        enable_mcp: 是否在启动时建立 MCP 会话。默认关闭，避免无谓拉起子进程。
        mcp_server_script: stdio 传输要拉起的 MCP Server 脚本路径（相对项目根）。
        collection_name: 知识库集合名。
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    api_key: str = ""
    max_concurrency: int = Field(default=8, ge=1)
    queue_timeout_seconds: float = Field(default=5.0, gt=0)
    rate_limit_per_minute: int = Field(default=60, ge=1)
    max_body_bytes: int = Field(default=262_144, ge=1)
    request_timeout_seconds: float = Field(default=120.0, gt=0)
    session_dir: Path = DEFAULT_SESSION_DIR
    enable_mcp: bool = False
    mcp_server_script: Path = DEFAULT_MCP_SERVER_SCRIPT
    collection_name: str = DEFAULT_COLLECTION

    @property
    def auth_enabled(self) -> bool:
        """是否启用 Bearer 认证（配了密钥就强制校验）。"""
        return bool(self.api_key)
