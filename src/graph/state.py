"""RequirementAnalysisWorkflow 的状态 Schema。

状态用 :class:`typing.TypedDict` 表达，``total=False`` 让调用方只需传入
``requirement_text`` 即可启动。每个节点只回写自己负责的字段（局部 patch），
并把节点名追加到 ``trace``，从而满足 Roadmap 第八周「执行路径可从 Trace 看清」。

所有字段的逐项说明在周四交付的 ``docs/week08_state_fields.md`` 中维护。
"""

from __future__ import annotations

from typing import TypedDict


class WorkflowState(TypedDict, total=False):
    """需求分析工作流的可观察状态。

    节点返回局部 patch 时，未出现的键保持不变；列表型字段（trace /
    human_answers / errors）由节点先读出旧值再追加，避免被整体覆盖。
    """

    requirement_text: str
    """原始需求文本（入口字段，由调用方写入）。"""
    category: str
    """classify 产出的需求分类（web / mobile / api / data / ai / ...）。"""
    confidence: float
    """classify 产出的置信度 0.0-1.0，用于歧义判定。"""
    needs_clarify: bool
    """classify 依据置信度与澄清问题得出的「是否需要人工澄清」标记。"""
    functional_points: list[str]
    """functional_points 节点产出的 2-6 条功能点。"""
    rag_context: list[dict]
    """rag_retrieve 产出的检索结果（chunk_id / source / text / score）。"""
    rag_degraded: bool
    """RAG 不可用或空召回时为 True，图继续不中断。"""
    risks: list[str]
    """risk 节点产出的 1-4 条风险。"""
    test_points: list[str]
    """test_points 节点产出的测试点（正常 / 异常 / 边界 / 回归）。"""
    clarification_questions: list[str]
    """classify 在歧义时产出的澄清问题，供 clarify 节点中断展示。"""
    human_answers: list[str]
    """人工补充内容，clarify 恢复后写入，参与后续节点生成。"""
    report: str
    """report 节点汇总的最终报告。"""
    errors: list[dict]
    """节点失败时写入 {node, type, message, attempt}，周三韧性逻辑填充。"""
    trace: list[str]
    """节点执行顺序，满足「执行路径可观察」。"""
    clarify_rounds: int
    """已进行的澄清轮次，防止无限澄清。"""
