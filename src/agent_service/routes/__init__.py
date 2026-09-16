"""服务路由包。

每个子模块只负责一层能力：``health`` 是运维探针，``rag`` 对知识库问答，
``workflow`` 对需求分析工作流，``tools`` 对 MCP 工具调用。
"""

from .health import router as health_router
from .rag import router as rag_router
from .tools import router as tools_router
from .workflow import router as workflow_router

__all__ = [
    "health_router",
    "rag_router",
    "tools_router",
    "workflow_router",
]
