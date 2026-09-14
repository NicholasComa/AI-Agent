"""服务路由包。

每个子模块只负责一层能力：``health`` 是运维探针，后续 ``rag`` /
``workflow`` / ``tools`` 分别对 RAG 检索、工作流编排与 MCP 工具。
"""

from .health import router as health_router

__all__ = ["health_router"]
