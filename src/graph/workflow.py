"""组装 RequirementAnalysisWorkflow：7 节点 + 条件边 + 可注入 Checkpoint。

图结构（固定边）：:

    START -> classify
    classify -[needs_clarify]-> clarify -> functional_points
    classify -[else]-> functional_points
    functional_points -> rag_retrieve -> risk -> test_points -> report -> END

``clarify`` 内部用 :func:`langgraph.types.interrupt` 挂起，调用方用
:class:`langgraph.types.Command` 携带人工答复再次进入，图从挂起点续跑。

依赖（chat_fn / rag / config）与 checkpointer 全部由外部注入，默认分别取
Fake ChatFn、None（RAG 降级）、环境变量配置、进程内 InMemorySaver——测试与
真实链路都不写死。
"""

from __future__ import annotations

from functools import partial

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .config import WorkflowConfig
from .fakes import make_fake_chat
from .nodes import (
    Deps,
    clarify,
    classify,
    functional_points,
    rag_retrieve,
    report,
    risk,
    route_after_classify,
    test_points,
)
from .state import WorkflowState


def build_requirement_workflow(
    *,
    chat_fn: object | None = None,
    rag: object | None = None,
    config: WorkflowConfig | None = None,
    checkpointer: object | None = None,
) -> CompiledStateGraph:
    """编译需求分析工作流。

    Args:
        chat_fn: 异步对话函数（messages -> text）。缺省注入 Fake，便于不接模型跑通。
        rag: 可选 RAG 检索器；为 None 时 rag_retrieve 直接降级。
        config: 运行参数；缺省读 ``JWIPC_GRAPH_*`` 环境变量。
        checkpointer: Checkpoint 保存器；缺省 InMemorySaver。测试可注入 SqliteSaver。

    Returns:
        已编译的图；调用时必须传 ``{"configurable": {"thread_id": ...}}``。
    """
    cfg = config or WorkflowConfig.from_env()
    chat = chat_fn or make_fake_chat()
    deps = Deps(chat_fn=chat, rag=rag, config=cfg)

    graph = StateGraph(WorkflowState)
    graph.add_node("classify", partial(classify, deps=deps))
    graph.add_node("clarify", partial(clarify, deps=deps))
    graph.add_node("functional_points", partial(functional_points, deps=deps))
    graph.add_node("rag_retrieve", partial(rag_retrieve, deps=deps))
    graph.add_node("risk", partial(risk, deps=deps))
    graph.add_node("test_points", partial(test_points, deps=deps))
    graph.add_node("report", report)  # 纯格式化节点，无 deps 参数

    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        route_after_classify,
        {"clarify": "clarify", "functional_points": "functional_points"},
    )
    graph.add_edge("clarify", "functional_points")
    graph.add_edge("functional_points", "rag_retrieve")
    graph.add_edge("rag_retrieve", "risk")
    graph.add_edge("risk", "test_points")
    graph.add_edge("test_points", "report")
    graph.add_edge("report", END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver())
