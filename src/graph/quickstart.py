"""最小状态图：条件分支、人工中断与 Checkpoint 续跑。

图结构为 ``START -> classify -> (clarify | final) -> END``。``classify``
判断输入是否以问号结尾，是则进入 ``clarify``，由
:func:`langgraph.types.interrupt` 抛出澄清请求并挂起整个图；调用方用
:class:`langgraph.types.Command` 携带人工答复再次调用，图从挂起的那一步
继续执行，而不是从头重跑。
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

CLARIFY_NODE = "clarify"
"""需要人工补充时进入的节点名。"""

FINAL_NODE = "final"
"""无需人工补充时进入的节点名。"""


class QuickstartState(TypedDict):
    """最小图的状态：输入问题、是否歧义、最终答复。"""

    question: str
    ambiguous: bool
    answer: str


def classify(state: QuickstartState) -> dict:
    """判定输入是否含歧义：以问号结尾视为需要澄清。

    同时接受半角 ``?`` 与全角 ``？``，中文输入下两者都会出现。
    """
    return {"ambiguous": state["question"].rstrip().endswith(("?", "？"))}


def route_after_classify(state: QuickstartState) -> str:
    """按歧义标记返回下游节点名。"""
    return CLARIFY_NODE if state["ambiguous"] else FINAL_NODE


def clarify(state: QuickstartState) -> dict:
    """挂起并等待人工补充，恢复后把答复写入状态。

    :func:`interrupt` 会抛出图级别的中断信号，函数在此处不会继续往下执行；
    直到调用方用 ``Command(resume=...)`` 再次调用，其返回值才是人工输入。
    """
    reply = interrupt({"questions": [f"请补充说明：{state['question']}"]})
    return {"answer": f"已补充后答复：{reply}"}


def final(state: QuickstartState) -> dict:
    """无歧义时直接给出答复。"""
    return {"answer": f"直接答复：{state['question']}"}


def build_quickstart_graph() -> CompiledStateGraph:
    """编译最小图，并挂载进程内的 Checkpoint 保存器。

    Returns:
        已编译的图；调用时必须传入
        ``{"configurable": {"thread_id": ...}}``，否则 Checkpoint 无处存放。
    """
    graph = StateGraph(QuickstartState)
    graph.add_node("classify", classify)
    graph.add_node(CLARIFY_NODE, clarify)
    graph.add_node(FINAL_NODE, final)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        route_after_classify,
        {CLARIFY_NODE: CLARIFY_NODE, FINAL_NODE: FINAL_NODE},
    )
    graph.add_edge(CLARIFY_NODE, END)
    graph.add_edge(FINAL_NODE, END)
    return graph.compile(checkpointer=InMemorySaver())
