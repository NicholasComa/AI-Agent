"""指标库：把「实际产出 + 标注」算成一个可判定的 :class:`MetricResult`。

设计约束
--------

1. **纯函数**。全部指标只吃普通 Python 值（列表、字符串、布尔），不接网络、
   不读文件、不碰数据库。这样每个指标都能用参数化测试覆盖到边界取值，也
   能在没有 Qdrant / Ollama 的机器上跑完。
2. **可跳过**。有些检查项对某条用例天然不适用（例如拒答题没有期望来源，
   ``retrieval_hit`` 无从比对）。此时返回 ``skipped=True`` 而**不是** ``passed=False``
   —— 把「不适用」记成「失败」会让通过率失去解释力，报告里也分不清该修哪一边。
3. **延迟与 token 的口径统一走观测层**。延迟分位数的桶边界直接复用
   :data:`agent_service.metrics.LATENCY_BUCKETS_MS`，避免评测报告与运行时
   监控给出两套无法对照的数字。

检查项语法
----------

数据集里的 ``checks`` 是字符串，两种写法：

- ``retrieval_hit@3`` —— ``@`` 后是 Top-K 的 K；
- ``answer_point_coverage>=0.6`` —— ``>=`` 后是判定阈值。

没有后缀时用指标自身的缺省参数。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

LATENCY_BUCKETS_MS: tuple[int, ...] = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10_000)
"""延迟分位数的桶上界（毫秒）。

与 :mod:`agent_service.metrics` 的同名常量保持一致的**取值**；这里复制一份常量
而不是跨包导入，是因为评测层要能在不启动服务层的前提下单独运行。两者取值若
出现分歧，``tests/evaluation`` 里有一条用例专门比对。
"""

SCENARIO_METRICS: dict[str, tuple[str, ...]] = {
    "knowledge_qa": (
        "retrieval_hit",
        "citation_source_hit",
        "answer_point_coverage",
        "refusal_correct",
    ),
    "requirement_analysis": (
        "category_match",
        "clarification_expected",
        "functional_coverage",
        "schema_valid_rate",
    ),
    "tool_call": (
        "tool_selected",
        "args_schema_valid",
        "outcome_match",
        "deny_enforced",
    ),
}
"""各场景允许出现在 ``checks`` 里的指标名。"""

AGGREGATE_METRICS: tuple[str, ...] = (
    "pass_rate",
    "latency_p50",
    "latency_p95",
    "total_tokens",
    "cost_usd",
)
"""跨场景的汇总指标，不写进单条用例的 ``checks``。"""

_CHECK_RE = re.compile(
    r"^(?P<name>[a-z][a-z0-9_]*)(?:@(?P<k>\d+))?(?:>=(?P<threshold>\d+(?:\.\d+)?))?$"
)
"""``name@K`` / ``name````>=阈值`` 的解析式。"""


class MetricResult(BaseModel):
    """单条检查项的判定结果。

    Attributes:
        name: 指标名；带参数的会把参数拼进名字（如 ``retrieval_hit@3``），
            便于报告里直接展示用的是哪一档。
        passed: 是否通过。``skipped`` 为真时该值无意义，不应计入通过率。
        value: 数值结果（比例、计数、秒数）；无自然取值时为 ``None``。
        detail: 一行人类可读说明，写清「比了什么、差在哪」。
        skipped: 该项是否因不适用而未评估。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    value: float | None = None
    detail: str = ""
    skipped: bool = False

    @classmethod
    def skipped_result(cls, name: str, detail: str) -> Self:
        """构造一个「不适用」的结果。"""
        return cls(name=name, passed=False, value=None, detail=detail, skipped=True)

    def render(self) -> str:
        """渲染成报告里的一行。"""
        if self.skipped:
            return f"{self.name}=skip ({self.detail})"
        mark = "pass" if self.passed else "FAIL"
        suffix = f" value={self.value:.4g}" if self.value is not None else ""
        note = f" — {self.detail}" if self.detail else ""
        return f"{self.name}={mark}{suffix}{note}"


@dataclass(frozen=True)
class CheckSpec:
    """解析后的检查项。"""

    raw: str
    name: str
    k: int | None = None
    threshold: float | None = None


def parse_check(raw: str) -> CheckSpec:
    """把 ``checks`` 里的一条字符串解析成 :class:`CheckSpec`。

    Raises:
        ValueError: 语法不认识（例如写成 ``hit@k`` 这类非数字参数）。
    """
    text = raw.strip()
    match = _CHECK_RE.match(text)
    if match is None:
        raise ValueError(f"无法识别的检查项写法：{raw!r}")
    return CheckSpec(
        raw=text,
        name=match.group("name"),
        k=int(match.group("k")) if match.group("k") else None,
        threshold=float(match.group("threshold")) if match.group("threshold") else None,
    )


def check_supported(spec: CheckSpec, scenario: str) -> bool:
    """该检查项是否属于此场景的指标集。"""
    allowed = SCENARIO_METRICS.get(scenario, ())
    if spec.name in AGGREGATE_METRICS:
        return False
    return spec.name in allowed


def _normalize(text: str) -> str:
    """归一化文本后再做要点包含判断。

    去掉空白与常见中英文标点、统一大小写。要点匹配是**子串包含**而非语义
    等价，所以先抹平排版差异，避免「答案里有这个要点，只因多了个空格或句号
    就被判没命中」这种假失败。
    """
    lowered = text.lower()
    return re.sub(r"[\s，。、；：！？,.;:!?\"'`（）()\[\]【】—\-]+", "", lowered)


class PointCoverage(BaseModel):
    """要点覆盖的明细，供报告里解释「缺了哪几条」。"""

    model_config = ConfigDict(extra="forbid")

    matched: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

    @property
    def coverage(self) -> float:
        """命中比例；期望要点为空时计 1.0。"""
        total = len(self.matched) + len(self.missing)
        return 1.0 if total == 0 else len(self.matched) / total


def point_coverage(answer: str, expect_points: Sequence[str]) -> PointCoverage:
    """算期望要点在答案里的覆盖情况。"""
    haystack = _normalize(answer)
    matched: list[str] = []
    missing: list[str] = []
    for point in expect_points:
        needle = _normalize(point)
        if needle and needle in haystack:
            matched.append(point)
        else:
            missing.append(point)
    return PointCoverage(matched=matched, missing=missing)


# ----------------------------------------------------------------------
# 知识问答
# ----------------------------------------------------------------------


def retrieval_hit(
    sources: Sequence[str],
    expect_sources: Sequence[str],
    *,
    k: int = 3,
) -> MetricResult:
    """Top-K 内是否出现期望来源文件名。

    Args:
        sources: 实际召回的来源列表，**按相似度降序**。
        expect_sources: 期望来源集合，命中任一即算通过。
        k: 只看前 K 条。
    """
    name = f"retrieval_hit@{k}"
    if not expect_sources:
        return MetricResult.skipped_result(name, "标注里没有期望来源，无从比对")
    top = [str(item) for item in list(sources)[:k]]
    hits = [item for item in top if item in set(expect_sources)]
    detail = f"命中 {hits}；Top-{k}={top}" if hits else f"Top-{k} 未命中，实际={top}"
    return MetricResult(name=name, passed=bool(hits), value=1.0 if hits else 0.0, detail=detail)


def citation_source_hit(citations: Sequence[str], expect_sources: Sequence[str]) -> MetricResult:
    """回答里引用的来源是否命中期望来源。"""
    if not expect_sources:
        return MetricResult.skipped_result("citation_source_hit", "标注里没有期望来源，无从比对")
    actual = [str(item) for item in citations]
    hits = [item for item in actual if item in set(expect_sources)]
    detail = f"引用命中 {hits}；实际引用={actual}" if hits else f"引用未命中，实际={actual}"
    return MetricResult(
        name="citation_source_hit", passed=bool(hits), value=1.0 if hits else 0.0, detail=detail
    )


def answer_point_coverage(
    answer: str,
    expect_points: Sequence[str],
    *,
    threshold: float = 0.6,
) -> MetricResult:
    """期望要点命中数 ÷ 期望要点数，与阈值比较。"""
    coverage = point_coverage(answer, expect_points)
    if not expect_points:
        return MetricResult.skipped_result("answer_point_coverage", "标注里没有期望要点")
    detail = f"覆盖 {len(coverage.matched)}/{len(expect_points)}"
    if coverage.missing:
        detail += f"；缺 {coverage.missing}"
    return MetricResult(
        name="answer_point_coverage",
        passed=coverage.coverage >= threshold,
        value=coverage.coverage,
        detail=detail,
    )


def refusal_correct(has_answer: bool, expect_found: bool) -> MetricResult:
    """``has_answer`` 与 ``expect_found`` 是否一致。"""
    return MetricResult(
        name="refusal_correct",
        passed=has_answer is expect_found,
        value=1.0 if has_answer is expect_found else 0.0,
        detail=f"has_answer={has_answer}，标注 expect_found={expect_found}",
    )


# ----------------------------------------------------------------------
# 需求拆解
# ----------------------------------------------------------------------


def category_match(actual: str | None, expect: str | None) -> MetricResult:
    """分类结果与标注是否一致。"""
    if expect is None:
        return MetricResult.skipped_result("category_match", "标注未规定分类")
    return MetricResult(
        name="category_match",
        passed=(actual or "") == expect,
        value=1.0 if (actual or "") == expect else 0.0,
        detail=f"实际={actual!r}，标注={expect!r}",
    )


def clarification_expected(actual: bool, expect: bool) -> MetricResult:
    """是否需要澄清的判断与标注是否一致。"""
    return MetricResult(
        name="clarification_expected",
        passed=actual is expect,
        value=1.0 if actual is expect else 0.0,
        detail=f"判断需澄清={actual}，标注={expect}",
    )


def functional_coverage(
    actual_points: Sequence[str],
    expect_points: Sequence[str],
    *,
    threshold: float = 0.6,
) -> MetricResult:
    """标注主题命中数 ÷ 标注主题数。

    与知识问答的要点覆盖同构，但作用在功能点列表上：只要**实际列出的功能点里
    出现过**该标注主题，就算命中。方向是「标注主题被覆盖」，不是「实际功能点
    全部正确」——后者需要语义判定，属于 Rubric 的职责。
    """
    if not expect_points:
        return MetricResult.skipped_result("functional_coverage", "标注里没有期望主题")
    haystack = _normalize("\n".join(str(item) for item in actual_points))
    matched = [p for p in expect_points if _normalize(p) and _normalize(p) in haystack]
    missing = [p for p in expect_points if p not in matched]
    value = len(matched) / len(expect_points)
    detail = f"覆盖 {len(matched)}/{len(expect_points)}"
    if missing:
        detail += f"；缺 {missing}"
    return MetricResult(
        name="functional_coverage", passed=value >= threshold, value=value, detail=detail
    )


def schema_valid_rate(valid: bool) -> MetricResult:
    """节点产出能否通过 Pydantic 校验。"""
    return MetricResult(
        name="schema_valid_rate",
        passed=bool(valid),
        value=1.0 if valid else 0.0,
        detail="产出通过 schema 校验" if valid else "产出未通过 schema 校验",
    )


# ----------------------------------------------------------------------
# 工具调用
# ----------------------------------------------------------------------


def tool_selected(actual: str | None, expect: str) -> MetricResult:
    """落到的工具与期望是否一致。"""
    return MetricResult(
        name="tool_selected",
        passed=(actual or "") == expect,
        value=1.0 if (actual or "") == expect else 0.0,
        detail=f"实际={actual!r}，期望={expect!r}",
    )


def args_schema_valid(valid: bool, detail: str = "") -> MetricResult:
    """参数能否通过工具 schema 校验。"""
    return MetricResult(
        name="args_schema_valid",
        passed=bool(valid),
        value=1.0 if valid else 0.0,
        detail=detail or ("参数合法" if valid else "参数未通过 schema 校验"),
    )


def outcome_match(actual: str, expect: str, *, kind: str | None = None) -> MetricResult:
    """成功 / 被拒 与标注是否一致。

    Args:
        actual: 实际结局。
        expect: 标注结局。
        kind: 工具返回的类别（如 ``forbidden``）。只写进 ``detail`` 供人排查，
            不参与判定——判定标准是结局本身，不是拒绝理由的分类。
    """
    detail = f"实际={actual}，标注={expect}"
    if kind:
        detail += f"（kind={kind}）"
    return MetricResult(
        name="outcome_match",
        passed=actual == expect,
        value=1.0 if actual == expect else 0.0,
        detail=detail,
    )


def deny_enforced(actual: str, *, expect_denied: bool, kind: str | None = None) -> MetricResult:
    """白名单外调用是否被拒。

    只在「期望被拒」的用例上有意义：期望成功时该项 skip，否则它会把正常
    调用也拉进分母，让「越权拦截率」这个数字失去含义。
    """
    if not expect_denied:
        return MetricResult.skipped_result("deny_enforced", "该用例期望正常返回，越权拦截不适用")
    detail = f"实际={actual}，期望=denied"
    if kind:
        detail += f"（kind={kind}）"
    return MetricResult(
        name="deny_enforced",
        passed=actual == "denied",
        value=1.0 if actual == "denied" else 0.0,
        detail=detail,
    )


# ----------------------------------------------------------------------
# 全场景汇总
# ----------------------------------------------------------------------


def percentile_from_buckets(
    values: Sequence[float],
    quantile: float,
    *,
    buckets: Sequence[int] = LATENCY_BUCKETS_MS,
) -> float | None:
    """按给定桶边界算分位数，返回**所在桶的上界**。

    用直方图而不是排序取值，是为了与运行时监控口径一致：运行时只有桶计数，
    没有原始样本。代价是结果被量化到桶边界（例如 137ms 会报成 250ms），这是
    刻意接受的口径统一，而不是精度缺陷。

    Args:
        values: 原始样本（毫秒）。
        quantile: 分位点，``0.5`` 表示中位数。
        buckets: 桶上界，需升序。

    Returns:
        桶上界；样本为空时返回 ``None``。
    """
    samples = sorted(float(v) for v in values)
    if not samples:
        return None
    if not 0.0 < quantile <= 1.0:
        raise ValueError(f"分位点需落在 (0, 1]，收到 {quantile}")
    # 秩的取法沿用「向上取整」：n=5、q=0.5 取第 3 个样本，与「至少一半样本
    # 不超过该值」的定义一致。取 floor 会让 p50 在偶数样本时偏小。
    rank = max(1, min(len(samples), int(-(-len(samples) * quantile // 1))))
    target = samples[rank - 1]
    for bound in sorted(buckets):
        if target <= bound:
            return float(bound)
    return float(target)


def aggregate_usage(usages: Iterable[Mapping[str, Any] | None]) -> dict[str, float]:
    """汇总 token 与成本。

    ``cost_usd`` 按单价表折算；本地模型单价为 0，因此本地跑出来的成本是 0.0
    而不是「未知」——报告里要写明这一点，否则 0 会被误读成「没统计」。
    """
    input_tokens = 0
    output_tokens = 0
    cost = 0.0
    for usage in usages:
        if not usage:
            continue
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
        cost += float(usage.get("cost_usd") or 0.0)
    return {
        "input_tokens": float(input_tokens),
        "output_tokens": float(output_tokens),
        "total_tokens": float(input_tokens + output_tokens),
        "cost_usd": cost,
    }


__all__ = [
    "AGGREGATE_METRICS",
    "LATENCY_BUCKETS_MS",
    "SCENARIO_METRICS",
    "CheckSpec",
    "MetricResult",
    "PointCoverage",
    "aggregate_usage",
    "answer_point_coverage",
    "args_schema_valid",
    "category_match",
    "check_supported",
    "citation_source_hit",
    "clarification_expected",
    "deny_enforced",
    "functional_coverage",
    "outcome_match",
    "parse_check",
    "percentile_from_buckets",
    "point_coverage",
    "refusal_correct",
    "retrieval_hit",
    "schema_valid_rate",
    "tool_selected",
]
