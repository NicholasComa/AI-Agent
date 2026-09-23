"""被测系统的**产出契约**：把真实产出规约成指标可判定的形状。

为什么单独一个模块
------------------

``metrics`` 是纯函数，输入是普通值（列表、布尔、字符串）；``dataset`` 描述的是
**标注**。但把「工作流返回的那个 dict」和「MCP 工具返回的那段结构化内容」直接
喂给指标，会让「产出长什么样」这件事散落在执行脚本里，既无法单测，也容易在
两个场景间出现两套判断标准。

本模块专门做这一层收敛：只回答两个问题——

1. 工作流产出是否满足它自己声明的字段契约（喂给 ``schema_valid_rate``）；
2. 工具调用的结果是正常返回、被沙箱拒绝、执行失败，还是业务校验不通过
   （喂给 ``outcome_match`` / ``deny_enforced``）。

判定**不用** ``agent_service.schemas.RequirementAnalysis``：那是 Week 2 的
接口契约，要求自带 ``title`` 字段，而工作流的状态里没有这一项。拿一个更严的
模型去校验一个更宽的产出，只会让每一条用例都以同一个理由失败。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DENY_KINDS: tuple[str, ...] = ("forbidden", "bad_path", "argument_rejected")
"""沙箱与命令白名单的拒绝类别。

``forbidden`` 是路径穿越或越出沙箱，``bad_path`` 是绝对路径或空字节，
``argument_rejected`` 是 git 子命令/参数不在白名单内。三者都表示「被策略拒绝」，
与「执行出错」（``git_error`` / ``internal``）和「目标不存在」（``not_found``）
是不同性质，必须分开。
"""

FAIL_KINDS: tuple[str, ...] = ("git_error", "internal", "empty_message")
"""工具执行层面的失败类别。"""

GATE_KINDS: tuple[str, ...] = ("needs_confirmation",)
"""确认门拦下的类别。

与 :data:`DENY_KINDS` 分开：确认门不是「不允许」，而是「等你点头」，它拦下的
调用在一次人工确认后本可以成功。把它算进越权拦截率会虚高。
"""

OUTCOME_OK = "ok"
OUTCOME_DENIED = "denied"
OUTCOME_FAILED = "failed"
OUTCOME_INVALID = "invalid"
OUTCOME_GATED = "needs_confirmation"


class WorkflowOutcome:
    """工作流产出经过规约后的可判定视图。

    Attributes:
        category: 分类结果。
        confidence: 置信度，已夹到 ``[0, 1]``。
        needs_clarify: 是否需要人工澄清。
        clarification_questions: 澄清问题列表。
        functional_points: 功能点列表。
        risks: 风险列表。
        test_points: 测试点列表。
        interrupted: 本次执行是否在 clarify 节点中断。中断是用例「期望澄清」
            时的正常路径，不是异常。
    """

    __slots__ = (
        "category",
        "clarification_questions",
        "confidence",
        "functional_points",
        "interrupted",
        "needs_clarify",
        "risks",
        "test_points",
    )

    def __init__(
        self,
        *,
        category: str,
        confidence: float,
        needs_clarify: bool,
        clarification_questions: list[str],
        functional_points: list[str],
        risks: list[str],
        test_points: list[str],
        interrupted: bool,
    ) -> None:
        self.category = category
        self.confidence = confidence
        self.needs_clarify = needs_clarify
        self.clarification_questions = clarification_questions
        self.functional_points = functional_points
        self.risks = risks
        self.test_points = test_points
        self.interrupted = interrupted

    def __repr__(self) -> str:
        return (
            f"WorkflowOutcome(category={self.category!r}, confidence={self.confidence:.2f}, "
            f"needs_clarify={self.needs_clarify}, interrupted={self.interrupted})"
        )


def _as_str_list(value: Any) -> list[str] | None:
    """把值规约成「非空字符串列表」；不合规返回 ``None``。"""
    if not isinstance(value, list):
        return None
    if not all(isinstance(item, str) and item.strip() for item in value):
        return None
    return [item.strip() for item in value]


def workflow_outcome(state: Mapping[str, Any]) -> tuple[WorkflowOutcome | None, str]:
    """校验工作流状态并规约成 :class:`WorkflowOutcome`。

    校验项与「产出契约」一一对应，任何一项不满足都返回失败原因而不是抛异常——
    调用方要把原因写进报告，异常做不到这一点。

    Args:
        state: ``ainvoke`` 返回的状态字典（含可能的 ``__interrupt__``）。

    Returns:
        ``(规约结果, 失败原因)``；成功时失败原因为空串。
    """
    interrupted = "__interrupt__" in state

    category = state.get("category")
    if not isinstance(category, str) or not category.strip():
        return None, "category 缺失或不是非空字符串"

    confidence = state.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return None, "confidence 缺失或不是数值"
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        return None, f"confidence={confidence} 超出 [0, 1]"

    needs_clarify = state.get("needs_clarify", False)
    if not isinstance(needs_clarify, bool):
        return None, "needs_clarify 缺失或不是布尔"

    questions = _as_str_list(state.get("clarification_questions", []))
    if questions is None:
        return None, "clarification_questions 不是非空字符串列表"

    points = _as_str_list(state.get("functional_points", []))
    risks = _as_str_list(state.get("risks", []))
    tests = _as_str_list(state.get("test_points", []))
    if points is None or risks is None or tests is None:
        return None, "functional_points / risks / test_points 存在不合规取值"

    if not needs_clarify and not interrupted:
        # 走到 report 节点的用例必须三样齐全；只缺一样就说明某个节点静默降级了，
        # 这正是 schema_valid_rate 要抓的东西。
        missing = [
            name
            for name, value in (
                ("functional_points", points),
                ("risks", risks),
                ("test_points", tests),
            )
            if not value
        ]
        if missing:
            return None, f"非澄清路径缺少产出：{missing}"

    return (
        WorkflowOutcome(
            category=category.strip(),
            confidence=confidence,
            needs_clarify=needs_clarify,
            clarification_questions=questions,
            functional_points=points,
            risks=risks,
            test_points=tests,
            interrupted=interrupted,
        ),
        "",
    )


def tool_outcome(structured: Mapping[str, Any] | None) -> tuple[str, str | None]:
    """把工具的结构化返回规约成 ``(结局, 拒绝类别)``。

    判定顺序刻意如此：先看工具层是否成功，再看业务校验。反过来的话，
    ``check_commit_message`` 的「信息不合规」会被误记成工具失败。

    Args:
        structured: ``FastMCP.call_tool`` 返回的结构化内容。

    Returns:
        ``(outcome, kind)``。``kind`` 仅在 ``denied`` / ``failed`` / ``needs_confirmation``
        时有值。
    """
    if not structured:
        return OUTCOME_FAILED, None

    ok = structured.get("ok")
    kind = structured.get("kind")
    kind = str(kind) if kind else None

    if ok is False:
        if kind in DENY_KINDS:
            return OUTCOME_DENIED, kind
        if kind in GATE_KINDS:
            return OUTCOME_GATED, kind
        if kind in FAIL_KINDS:
            return OUTCOME_FAILED, kind
        # 未登记的 kind 一律当执行失败：把未知情况记成「被拒」会让拦截率虚高。
        return OUTCOME_FAILED, kind

    if structured.get("valid") is False:
        return OUTCOME_INVALID, None

    return OUTCOME_OK, None


__all__ = [
    "DENY_KINDS",
    "FAIL_KINDS",
    "GATE_KINDS",
    "OUTCOME_DENIED",
    "OUTCOME_FAILED",
    "OUTCOME_GATED",
    "OUTCOME_INVALID",
    "OUTCOME_OK",
    "WorkflowOutcome",
    "tool_outcome",
    "workflow_outcome",
]
