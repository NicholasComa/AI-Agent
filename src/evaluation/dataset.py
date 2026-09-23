"""Golden Dataset 的模型、读取与自检。

数据集是评测的**冻结基线**。冻结的含义是：报告里任何一个变化都必须能归因到
被评测的代码，而不能来自标注本身偷偷变了。为守住这一点，这里的校验刻意偏严：

1. ``extra="forbid"`` —— 把 ``expect_source`` 写成 ``expect_sources`` 之类的笔误
   直接报错，而不是被静默忽略后当成「这条没有期望来源」，进而让一个本该失败
   的用例悄悄通过。
2. ``input`` / ``reference`` 按 ``scenario`` 做**判别联合**——三个场景的特征字段
   完全不同，用一个大模型加一堆可选字段会让「写错场景的字段」无法被发现。
3. 跨字段一致性由 ``model_validator`` 兜住——``expect_found=true`` 却没有
   ``expect_sources``，是一条自相矛盾的标注。

加载入口是 :func:`load_cases`（逐行 JSONL），自检入口是 :func:`assert_dataset`。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from .metrics import check_supported, parse_check

SCENARIOS: tuple[str, ...] = ("knowledge_qa", "requirement_analysis", "tool_call")
"""三个评测场景。"""

SCENARIO_QUOTAS: dict[str, int] = {
    "knowledge_qa": 22,
    "requirement_analysis": 18,
    "tool_call": 12,
}
"""各场景的条数下限，合计 52 条。"""

TOTAL_MIN = 50
"""数据集总条数下限（计划要求「至少 50 条」）。"""

KNOWN_TOOLS: tuple[str, ...] = ("list_files", "read_file", "git_log", "check_commit_message")
"""MCP 服务当前注册的四个工具。越权用例也必须落在这四个之内。"""

OUTCOMES: tuple[str, ...] = ("ok", "denied", "failed", "invalid", "needs_confirmation")
"""工具调用的五种结局。

- ``ok`` / ``denied`` / ``failed`` 是工具层的结果（正常返回、被沙箱策略拒绝、执行报错）；
- ``needs_confirmation`` 是**确认门**拦下的调用：参数合法、也在沙箱内，但目标
  超过读取闸门，需要人工确认后才能继续。它与 ``denied`` 性质不同——前者是
  「策略不允许」，后者是「等你点头」；
- ``invalid`` 是工具正常返回、但业务校验判定为不合规，目前只有
  ``check_commit_message`` 会走到这一支。

合并任意两项都会让对应的统计失去含义：把确认门记成 ``ok`` 会以为拿到了文件，
记成 ``denied`` 会让「越权拦截率」虚高。
"""

_FORBID = ConfigDict(extra="forbid")


class DatasetError(ValueError):
    """数据集不满足一致性要求。"""


# ----------------------------------------------------------------------
# 三个场景的输入与标注
# ----------------------------------------------------------------------


class KnowledgeQaInput(BaseModel):
    """知识问答的输入。"""

    model_config = _FORBID

    query: str = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=20)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)


class KnowledgeQaReference(BaseModel):
    """知识问答的标注。"""

    model_config = _FORBID

    expect_found: bool
    expect_sources: list[str] = Field(default_factory=list)
    expect_points: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.expect_found and not self.expect_sources:
            raise ValueError("expect_found=true 时必须给出 expect_sources")
        if not self.expect_found and self.expect_sources:
            raise ValueError("expect_found=false（拒答题）不应给出 expect_sources")
        return self


class RequirementInput(BaseModel):
    """需求分析的输入。"""

    model_config = _FORBID

    text: str = Field(min_length=1)


class RequirementReference(BaseModel):
    """需求分析的标注。"""

    model_config = _FORBID

    expect_category: str | None = None
    expect_needs_clarify: bool
    expect_points: list[str] = Field(default_factory=list)


class ToolCallInput(BaseModel):
    """工具调用的输入。"""

    model_config = _FORBID

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_tool(self) -> Self:
        if self.tool not in KNOWN_TOOLS:
            raise ValueError(f"未知工具 {self.tool!r}，可选：{list(KNOWN_TOOLS)}")
        return self


class ToolCallReference(BaseModel):
    """工具调用的标注。"""

    model_config = _FORBID

    expect_tool: str
    expect_outcome: Literal["ok", "denied", "failed", "invalid", "needs_confirmation"]
    expect_deny_kind: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.expect_outcome == "denied" and self.expect_deny_kind is None:
            raise ValueError("expect_outcome=denied 时必须给出 expect_deny_kind")
        if self.expect_outcome != "denied" and self.expect_deny_kind is not None:
            raise ValueError("非 denied 的用例不应给出 expect_deny_kind")
        return self


# ----------------------------------------------------------------------
# 判别联合
# ----------------------------------------------------------------------


class _CaseBase(BaseModel):
    """三个场景共有的头部字段。"""

    model_config = _FORBID

    id: str = Field(min_length=1)
    group: str = Field(default="default", min_length=1)
    checks: list[str] = Field(min_length=1)
    note: str = ""


class KnowledgeQaCase(_CaseBase):
    """知识问答用例。"""

    scenario: Literal["knowledge_qa"]
    input: KnowledgeQaInput
    reference: KnowledgeQaReference


class RequirementCase(_CaseBase):
    """需求分析用例。"""

    scenario: Literal["requirement_analysis"]
    input: RequirementInput
    reference: RequirementReference


class ToolCallCase(_CaseBase):
    """工具调用用例。"""

    scenario: Literal["tool_call"]
    input: ToolCallInput
    reference: ToolCallReference


GoldenCase = Annotated[
    KnowledgeQaCase | RequirementCase | ToolCallCase,
    Field(discriminator="scenario"),
]
"""一条 Golden 用例；按 ``scenario`` 判别。"""

_CASE_ADAPTER: TypeAdapter[Any] = TypeAdapter(GoldenCase)


# ----------------------------------------------------------------------
# 读取
# ----------------------------------------------------------------------


def load_cases(path: str | Path) -> list[Any]:
    """逐行读 JSONL 并校验。

    Args:
        path: 数据集路径。

    Returns:
        校验通过的用例列表，顺序与文件一致。

    Raises:
        DatasetError: 文件缺失、为空，或任一行不合法。报错里带行号——标注是
            手写的，没有行号就只能靠肉眼数。
    """
    target = Path(path)
    if not target.exists():
        raise DatasetError(f"数据集不存在：{target}")
    cases: list[Any] = []
    for lineno, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{target}:{lineno} 不是合法 JSON：{exc}") from exc
        try:
            cases.append(_CASE_ADAPTER.validate_python(payload))
        except ValidationError as exc:
            raise DatasetError(f"{target}:{lineno} 校验失败：{exc}") from exc
    if not cases:
        raise DatasetError(f"数据集为空：{target}")
    return cases


def dump_cases(cases: list[Any], path: str | Path) -> Path:
    """把用例写成 JSONL（构建脚本用）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(case.model_dump(mode="json"), ensure_ascii=False, sort_keys=False)
        for case in cases
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


# ----------------------------------------------------------------------
# 自检
# ----------------------------------------------------------------------


def describe_counts(cases: list[Any]) -> dict[str, int]:
    """按场景统计条数。"""
    return dict(Counter(case.scenario for case in cases))


def describe_groups(cases: list[Any]) -> dict[str, int]:
    """按分组统计条数，键形如 ``knowledge_qa/core``。"""
    return dict(Counter(f"{case.scenario}/{case.group}" for case in cases))


def assert_dataset(path: str | Path) -> list[Any]:
    """数据集一致性自检，不通过抛 :class:`DatasetError`。

    检查项：id 唯一、场景条数达标、总数达标、``checks`` 语法可解析且属于该场景
    指标集。

    Args:
        path: 数据集路径。

    Returns:
        校验通过的用例列表。
    """
    cases = load_cases(path)
    problems: list[str] = []

    seen: dict[str, int] = {}
    for index, case in enumerate(cases, start=1):
        if case.id in seen:
            problems.append(f"id 重复：{case.id}（第 {seen[case.id]} 条与第 {index} 条）")
        seen.setdefault(case.id, index)

    counts = describe_counts(cases)
    for scenario, quota in SCENARIO_QUOTAS.items():
        actual = counts.get(scenario, 0)
        if actual < quota:
            problems.append(f"场景 {scenario} 只有 {actual} 条，少于要求的 {quota} 条")

    if len(cases) < TOTAL_MIN:
        problems.append(f"总条数 {len(cases)} 少于要求的 {TOTAL_MIN} 条")

    for case in cases:
        for raw in case.checks:
            try:
                spec = parse_check(raw)
            except ValueError as exc:
                problems.append(f"{case.id} 的检查项 {raw!r} 无法解析：{exc}")
                continue
            if not check_supported(spec, case.scenario):
                problems.append(f"{case.id}（{case.scenario}）的检查项 {raw!r} 不属于该场景指标集")

    if problems:
        raise DatasetError("数据集自检未通过：\n  - " + "\n  - ".join(problems))
    return cases


__all__ = [
    "KNOWN_TOOLS",
    "OUTCOMES",
    "SCENARIOS",
    "SCENARIO_QUOTAS",
    "TOTAL_MIN",
    "DatasetError",
    "GoldenCase",
    "KnowledgeQaCase",
    "KnowledgeQaInput",
    "KnowledgeQaReference",
    "RequirementCase",
    "RequirementInput",
    "RequirementReference",
    "ToolCallCase",
    "ToolCallInput",
    "ToolCallReference",
    "assert_dataset",
    "describe_counts",
    "describe_groups",
    "dump_cases",
    "load_cases",
]
