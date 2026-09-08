"""图编排包：基于 LangGraph 的状态化工作流。

提供两块内容：

- :mod:`graph.quickstart` —— 最小示例图，验证条件分支、人工中断与
  Checkpoint 续跑三条基本行为（周一交付）。
- :mod:`graph.workflow` —— 本周主线 ``RequirementAnalysisWorkflow``，
  6 个业务节点 + 1 个澄清中断节点（周二交付）。
"""

from .config import WorkflowConfig
from .fakes import ChatFn, make_fake_chat
from .quickstart import QuickstartState, build_quickstart_graph
from .state import WorkflowState
from .workflow import build_requirement_workflow

__all__ = [
    "WorkflowConfig",
    "WorkflowState",
    "ChatFn",
    "make_fake_chat",
    "QuickstartState",
    "build_quickstart_graph",
    "build_requirement_workflow",
]
