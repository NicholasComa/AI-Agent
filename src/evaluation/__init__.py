"""评测层：Golden Dataset、指标、Rubric 与报告。

分层
----

============  ==========================================================
模块           职责
============  ==========================================================
``dataset``   Golden Dataset 的 Pydantic 模型、JSONL 读取与一致性自检
``metrics``   纯函数指标库，输入「实际产出 + 标注」，输出可判定的结果
``rubric``    LLM-as-a-Judge 打分，重复判定并给一致率，调用纳入追踪
``report``    机器可读 JSON 与人类可读 Markdown 两份产物
============  ==========================================================

设计约束
--------

1. **离线可重复**。``metrics`` 是纯函数、不接网络；``dataset`` 只读本地文件。
   只有 ``rubric`` 会调模型，且它属于可选环节（``--judge off`` 时不参与）。
2. **不依赖服务层**。本包不导入 ``agent_service``——那会把 FastAPI 及其依赖
   拖进一个本该能在裸 Python 下跑完的模块。延迟桶边界因此在本包内保留一份
   常量，并由测试保证与 ``agent_service.metrics`` 取值一致。
3. **口径可追溯**。报告头部固定声明生成方式与检索方式，并给出失败用例对应的
   ``trace_id``，使每一个数字都能回到具体的 span。
"""

from __future__ import annotations

from .dataset import (
    KNOWN_TOOLS,
    SCENARIO_QUOTAS,
    SCENARIOS,
    TOTAL_MIN,
    DatasetError,
    GoldenCase,
    KnowledgeQaCase,
    KnowledgeQaInput,
    KnowledgeQaReference,
    RequirementCase,
    RequirementInput,
    RequirementReference,
    ToolCallCase,
    ToolCallInput,
    ToolCallReference,
    assert_dataset,
    describe_counts,
    describe_groups,
    dump_cases,
    load_cases,
)
from .metrics import (
    AGGREGATE_METRICS,
    LATENCY_BUCKETS_MS,
    SCENARIO_METRICS,
    CheckSpec,
    MetricResult,
    PointCoverage,
    aggregate_usage,
    answer_point_coverage,
    args_schema_valid,
    category_match,
    check_supported,
    citation_source_hit,
    clarification_expected,
    deny_enforced,
    functional_coverage,
    outcome_match,
    parse_check,
    percentile_from_buckets,
    point_coverage,
    refusal_correct,
    retrieval_hit,
    schema_valid_rate,
    tool_selected,
)
from .report import (
    TRACE_VIEW_COMMAND,
    CaseResult,
    EvalReport,
    ScenarioSummary,
    build_report,
    summarize,
)
from .rubric import (
    DEFAULT_REPEATS,
    JUDGE_MODEL_ENV,
    JUDGE_REPEATS_ENV,
    RubricResult,
    RubricScorer,
    judge_note,
    parse_score,
    resolve_judge_model,
    resolve_repeats,
)

__all__ = [
    "AGGREGATE_METRICS",
    "DEFAULT_REPEATS",
    "JUDGE_MODEL_ENV",
    "JUDGE_REPEATS_ENV",
    "KNOWN_TOOLS",
    "LATENCY_BUCKETS_MS",
    "SCENARIOS",
    "SCENARIO_METRICS",
    "SCENARIO_QUOTAS",
    "TOTAL_MIN",
    "TRACE_VIEW_COMMAND",
    "CaseResult",
    "CheckSpec",
    "DatasetError",
    "EvalReport",
    "GoldenCase",
    "KnowledgeQaCase",
    "KnowledgeQaInput",
    "KnowledgeQaReference",
    "MetricResult",
    "PointCoverage",
    "RequirementCase",
    "RequirementInput",
    "RequirementReference",
    "RubricResult",
    "RubricScorer",
    "ScenarioSummary",
    "ToolCallCase",
    "ToolCallInput",
    "ToolCallReference",
    "aggregate_usage",
    "answer_point_coverage",
    "args_schema_valid",
    "assert_dataset",
    "build_report",
    "category_match",
    "check_supported",
    "citation_source_hit",
    "clarification_expected",
    "deny_enforced",
    "describe_counts",
    "describe_groups",
    "dump_cases",
    "functional_coverage",
    "judge_note",
    "load_cases",
    "outcome_match",
    "parse_check",
    "parse_score",
    "percentile_from_buckets",
    "point_coverage",
    "refusal_correct",
    "resolve_judge_model",
    "resolve_repeats",
    "retrieval_hit",
    "schema_valid_rate",
    "summarize",
    "tool_selected",
]
