"""指标库测试：纯函数、只喂普通值，不接任何真实设施。"""

from __future__ import annotations

import pytest

from agent_service.metrics import LATENCY_BUCKETS_MS as SERVICE_BUCKETS
from evaluation.metrics import (
    AGGREGATE_METRICS,
    LATENCY_BUCKETS_MS,
    SCENARIO_METRICS,
    MetricResult,
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

# ----------------------------------------------------------------------
# 检查项语法
# ----------------------------------------------------------------------


def test_parse_check_reads_top_k():
    """``name@K`` 里的 K 被解析出来。"""
    spec = parse_check("retrieval_hit@3")

    assert spec.name == "retrieval_hit"
    assert spec.k == 3
    assert spec.threshold is None


def test_parse_check_reads_threshold():
    """``name>=T`` 里的 T 被解析出来。"""
    spec = parse_check("answer_point_coverage>=0.6")

    assert spec.name == "answer_point_coverage"
    assert spec.threshold == pytest.approx(0.6)
    assert spec.k is None


def test_parse_check_accepts_bare_name():
    """不带参数的写法合法。"""
    spec = parse_check("refusal_correct")

    assert spec.name == "refusal_correct"
    assert spec.k is None and spec.threshold is None


@pytest.mark.parametrize("raw", ["", "  ", "Retrieval_Hit", "retrieval_hit@k", "@3", "hit>=abc"])
def test_parse_check_rejects_bad_syntax(raw):
    """语法错误必须抛异常，而不是静默退化成某个默认指标。"""
    with pytest.raises(ValueError):
        parse_check(raw)


def test_check_supported_scopes_by_scenario():
    """检查项只在所属场景内有效。"""
    assert check_supported(parse_check("retrieval_hit@3"), "knowledge_qa")
    assert not check_supported(parse_check("retrieval_hit@3"), "tool_call")
    assert not check_supported(parse_check("pass_rate"), "knowledge_qa")


def test_scenario_metrics_cover_every_check_used_by_the_dataset():
    """数据集里用到的检查项都必须登记在场景指标表里。"""
    from evaluation_fakes import DATASET_PATH

    from evaluation.dataset import load_cases

    used = {parse_check(raw).name for case in load_cases(DATASET_PATH) for raw in case.checks}
    registered = {name for names in SCENARIO_METRICS.values() for name in names}

    assert used <= registered
    assert not (used & set(AGGREGATE_METRICS))


def test_latency_buckets_match_service_layer():
    """延迟桶边界必须与 ``agent_service.metrics`` 完全一致。

    评测层刻意复制了这份常量（避免把 FastAPI 拖进纯函数库），代价是可能出现
    分歧；这条用例就是那份代价的对价——一旦两边不一致，评测报告与运行时监控
    的数字就没法对照，而那种偏差很难从报告本身看出来。
    """
    assert LATENCY_BUCKETS_MS == SERVICE_BUCKETS


# ----------------------------------------------------------------------
# 知识问答
# ----------------------------------------------------------------------


def test_retrieval_hit_passes_when_expected_source_in_top_k():
    """期望来源落在 Top-K 内即通过。"""
    result = retrieval_hit(["a.md", "b.md", "c.md"], ["c.md"], k=3)

    assert result.passed
    assert result.name == "retrieval_hit@3"


def test_retrieval_hit_ignores_source_beyond_k():
    """排在第 K 名之后的来源不算命中。"""
    result = retrieval_hit(["a.md", "b.md", "c.md"], ["c.md"], k=2)

    assert not result.passed


def test_retrieval_hit_skips_without_expectation():
    """拒答题没有期望来源，应记 skip 而不是失败。"""
    result = retrieval_hit(["a.md"], [], k=3)

    assert result.skipped
    assert not result.passed


def test_citation_source_hit_reports_actual():
    """引用未命中时把实际引用写进说明，便于排查。"""
    result = citation_source_hit(["x.md"], ["rag_concepts.md"])

    assert not result.passed
    assert "x.md" in result.detail


def test_answer_point_coverage_normalizes_punctuation_and_space():
    """排版差异不应造成假失败。"""
    result = answer_point_coverage("检索 增强 生成。", ["检索增强生成"], threshold=0.6)

    assert result.passed


def test_answer_point_coverage_uses_threshold():
    """阈值决定通过与否：3 个要点命中 2 个。"""
    points = ["登录", "商品", "支付"]

    assert answer_point_coverage("登录与商品", points, threshold=0.6).passed
    assert not answer_point_coverage("登录与商品", points, threshold=0.7).passed


def test_answer_point_coverage_reports_missing_points():
    """缺失的要点要写进说明，否则「覆盖率为 0」看不出差在哪。"""
    coverage = point_coverage("完全无关的回答", ["登录", "支付"])

    assert coverage.missing == ["登录", "支付"]
    assert coverage.coverage == pytest.approx(0.0)


def test_point_coverage_empty_expectation_is_full():
    """期望要点为空时覆盖率记 1.0，避免除零。"""
    assert point_coverage("任意", []).coverage == pytest.approx(1.0)


def test_refusal_correct_matches_expectation():
    """拒答判定与标注一致才通过。"""
    assert refusal_correct(False, False).passed
    assert refusal_correct(True, True).passed
    assert not refusal_correct(True, False).passed


# ----------------------------------------------------------------------
# 需求拆解
# ----------------------------------------------------------------------


def test_category_match_skips_when_unannotated():
    """标注没规定分类时记 skip。"""
    assert category_match("web", None).skipped


def test_category_match_compares_exactly():
    """分类比对是精确相等，不做模糊匹配。"""
    assert category_match("web", "web").passed
    assert not category_match("mobile", "web").passed
    assert not category_match(None, "web").passed


def test_clarification_expected_compares_booleans():
    """澄清判断与标注一致才通过。"""
    assert clarification_expected(True, True).passed
    assert not clarification_expected(False, True).passed


def test_functional_coverage_counts_annotated_topics():
    """覆盖率按标注主题被命中的比例算，阈值是 ``>=`` 语义。"""
    raw = ["支持用户登录", "支持支付"]
    topics = ["登录", "支付", "报表"]

    # 2/3 ≈ 0.667，在 0.6 阈值下应当通过——checks 里写的就是 >=0.6。
    assert functional_coverage(raw, topics, threshold=0.6).passed

    result = functional_coverage(raw, topics, threshold=0.7)

    assert not result.passed
    assert result.value == pytest.approx(2 / 3)


def test_functional_coverage_skips_without_topics():
    """没有标注主题时记 skip。"""
    assert functional_coverage(["任意"], []).skipped


def test_schema_valid_rate_maps_boolean():
    """schema 校验结果是布尔。"""
    assert schema_valid_rate(True).passed
    assert not schema_valid_rate(False).passed


# ----------------------------------------------------------------------
# 工具调用
# ----------------------------------------------------------------------


def test_tool_selected_compares_names():
    """选中的工具与期望一致才通过。"""
    assert tool_selected("read_file", "read_file").passed
    assert not tool_selected("git_log", "read_file").passed


def test_args_schema_valid_keeps_custom_detail():
    """自定义说明被保留，便于记录参数为何不合规。"""
    result = args_schema_valid(False, "ValidationError: path 缺失")

    assert not result.passed
    assert "path 缺失" in result.detail


def test_outcome_match_includes_kind_in_detail():
    """拒绝类别只进说明，不参与判定。"""
    result = outcome_match("denied", "denied", kind="forbidden")

    assert result.passed
    assert "forbidden" in result.detail


def test_deny_enforced_only_applies_to_denied_cases():
    """期望正常返回的用例不该被算进越权拦截率。"""
    assert deny_enforced("ok", expect_denied=False).skipped
    assert deny_enforced("denied", expect_denied=True).passed
    assert not deny_enforced("ok", expect_denied=True).passed


# ----------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------


def test_percentile_returns_none_for_empty_sample():
    """空样本没有分位数。"""
    assert percentile_from_buckets([], 0.5) is None


@pytest.mark.parametrize(
    ("values", "quantile", "expected"),
    [
        ([5.0], 0.5, 10.0),
        ([12.0], 0.5, 25.0),
        ([120.0], 0.5, 250.0),
        ([12.0, 140.0, 900.0], 0.5, 250.0),
    ],
)
def test_percentile_quantizes_to_bucket_upper_bound(values, quantile, expected):
    """分位数被量化到桶上界——这是与运行时监控统一口径的代价。"""
    assert percentile_from_buckets(values, quantile) == expected


def test_percentile_rejects_invalid_quantile():
    """分位点必须落在 (0, 1]。"""
    with pytest.raises(ValueError):
        percentile_from_buckets([1.0], 0.0)


def test_percentile_beyond_last_bucket_returns_sample():
    """样本超过最大桶边界时返回样本本身，而不是被截断成桶上界。"""
    assert percentile_from_buckets([99_999.0], 0.95) == pytest.approx(99_999.0)


def test_aggregate_usage_sums_tokens_and_cost():
    """token 与成本都累加；缺失的条目按 0 处理。"""
    total = aggregate_usage(
        [
            {"input_tokens": 10, "output_tokens": 5},
            {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.002},
            None,
            {},
        ]
    )

    assert total["total_tokens"] == pytest.approx(17)
    assert total["cost_usd"] == pytest.approx(0.002)


def test_metric_result_render_shows_skip():
    """渲染结果里 skip 与 FAIL 必须能一眼分开。"""
    assert "skip" in MetricResult.skipped_result("m", "不适用").render()
    assert "FAIL" in MetricResult(name="m", passed=False, detail="x").render()
    assert "pass" in MetricResult(name="m", passed=True, value=1.0).render()
