"""评测层：报告渲染与 Rubric 判定。

报告是这一层唯一「给人看」的产物，测的重点不是数字算得对不对（那在
``test_evaluation_metrics`` 里），而是**报告有没有把口径和证据说清楚**：

- 头部必须声明 ``chat_mode`` / ``retrieval_mode``，否则不同口径的数字会被误读成回归；
- 失败清单每行必须带 ``trace_id``，并给出可直接执行的排查命令；
- 被跳过的检查项不能进分母，也不能在失败清单里冒出来；
- Rubric 的一一致率必须显式呈现，且解析失败不得被当成 0 分。
"""

from __future__ import annotations

import json

import pytest
from evaluation_fakes import constant_chat, make_chat

from evaluation.metrics import MetricResult
from evaluation.report import (
    TRACE_VIEW_COMMAND,
    CaseResult,
    build_report,
    summarize,
)
from evaluation.rubric import (
    DEFAULT_REPEATS,
    JUDGE_MODEL_ENV,
    JUDGE_REPEATS_ENV,
    MAX_REPEATS,
    RubricResult,
    RubricScorer,
    build_messages,
    dumps,
    judge_note,
    parse_score,
    resolve_judge_model,
    resolve_repeats,
)

# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def metric(name: str, passed: bool, *, skipped: bool = False, detail: str = "") -> MetricResult:
    """造一条检查结果。"""
    return MetricResult(name=name, passed=passed, skipped=skipped, detail=detail)


def case(
    case_id: str,
    scenario: str,
    *,
    group: str = "core",
    passed: bool = True,
    metrics: list[MetricResult] | None = None,
    trace_id: str | None = "t-1",
    latency_ms: float | None = 100.0,
    error: str | None = None,
) -> CaseResult:
    """造一条用例结果。"""
    return CaseResult(
        id=case_id,
        scenario=scenario,
        group=group,
        passed=passed,
        trace_id=trace_id,
        latency_ms=latency_ms,
        metrics=metrics if metrics is not None else [metric("category_match", passed)],
        error=error,
    )


def sample_report(*, scenario: str | None = None, notes: list[str] | None = None):
    """一份含失败用例的报告。"""
    cases = [
        case("qa-001", "knowledge_qa", metrics=[metric("retrieval_hit@3", True)], latency_ms=80),
        case(
            "qa-002",
            "knowledge_qa",
            passed=False,
            metrics=[
                metric("retrieval_hit@3", False, detail="命中 0/1"),
                metric("citation_source_hit", True),
            ],
            latency_ms=120,
            trace_id="trace-qa-002",
        ),
        case("tool-001", "tool_call", group="denied", latency_ms=40),
    ]
    return build_report(
        cases,
        tag="dryrun",
        dataset="data/golden/week10_golden.jsonl",
        dataset_size=52,
        chat_mode="fake",
        retrieval_mode="vector",
        trace_dir="logs/eval/traces",
        scenario=scenario,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# CaseResult
# --------------------------------------------------------------------------- #


def test_failed_metrics_excludes_skipped() -> None:
    """被跳过的检查项不算失败。"""
    row = case(
        "x",
        "tool_call",
        passed=False,
        metrics=[
            metric("tool_selected", True),
            metric("outcome_match", False),
            metric("args_schema_valid", False, skipped=True),
        ],
    )
    assert [item.name for item in row.failed_metrics] == ["outcome_match"]
    assert [item.name for item in row.skipped_metrics] == ["args_schema_valid"]


def test_case_result_rejects_unknown_field() -> None:
    """多余字段直接报错，防止报告结构悄悄漂移。"""
    with pytest.raises(ValueError):
        CaseResult(id="x", scenario="tool_call", group="ok", passed=True, extra=1)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# summarize
# --------------------------------------------------------------------------- #


def test_summarize_groups_by_scenario_and_averages_metrics() -> None:
    """按场景分组，单项通过率不含跳过的判定。"""
    cases = [
        case("a", "knowledge_qa", metrics=[metric("retrieval_hit@3", True)]),
        case("b", "knowledge_qa", passed=False, metrics=[metric("retrieval_hit@3", False)]),
        case("c", "knowledge_qa", metrics=[metric("retrieval_hit@3", False, skipped=True)]),
    ]
    summaries = {item.scenario: item for item in summarize(cases)}

    qa = summaries["knowledge_qa"]
    assert (qa.total, qa.passed, qa.failed) == (3, 2, 1)
    assert qa.skipped_metrics == 1
    # 分母是 2（跳过的那条不进分母），不是 3
    assert qa.metric_pass_rates["retrieval_hit@3"] == pytest.approx(0.5)


def test_summarize_can_filter_one_scenario() -> None:
    """``scenario`` 非空时只汇总该场景。"""
    cases = [case("a", "knowledge_qa"), case("b", "tool_call")]
    assert [item.scenario for item in summarize(cases, scenario="tool_call")] == ["tool_call"]


def test_summarize_latency_quantized_to_bucket_upper_bound() -> None:
    """延迟按桶上界量化，与运行期直方图口径一致。"""
    cases = [case(str(i), "tool_call", latency_ms=value) for i, value in enumerate([12.0, 30.0])]
    summary = summarize(cases)[0]
    assert (summary.latency_p50, summary.latency_p95) == (25.0, 50.0)


def test_summarize_handles_case_without_latency() -> None:
    """没有延迟记录的用例不参与分位计算，也不报错。"""
    summary = summarize([case("a", "tool_call", latency_ms=None)])[0]
    assert summary.latency_p50 is None
    assert summary.latency_p95 is None


def test_summarize_empty_input() -> None:
    """空输入返回空列表。"""
    assert summarize([]) == []


# --------------------------------------------------------------------------- #
# build_report
# --------------------------------------------------------------------------- #


def test_build_report_totals_and_metadata() -> None:
    """总计与口径字段齐全。"""
    report = sample_report(notes=["chat=fake"])
    assert report.totals["用例数"] == 3
    assert report.totals["通过"] == 2
    assert report.totals["失败"] == 1
    assert report.totals["pass_rate"] == pytest.approx(2 / 3)
    assert report.chat_mode == "fake"
    assert report.retrieval_mode == "vector"
    assert report.dataset_size == 52
    assert report.notes == ["chat=fake"]
    assert report.generated_at


def test_build_report_usage_optional() -> None:
    """给了 usage 才写 token 与成本。"""
    without = sample_report()
    assert "total_tokens" not in without.totals

    cases = [case("a", "tool_call")]
    with_usage = build_report(
        cases,
        tag="t",
        dataset="d",
        dataset_size=1,
        chat_mode="fake",
        retrieval_mode="vector",
        trace_dir="logs/eval/traces",
        usage={"total_tokens": 1234.0, "cost_usd": 0.02},
    )
    assert with_usage.totals["total_tokens"] == 1234.0
    assert with_usage.totals["cost_usd"] == pytest.approx(0.02)


def test_build_report_scenario_filter_propagates() -> None:
    """``scenario`` 一路传到 summarize。"""
    report = sample_report(scenario="tool_call")
    assert [item.scenario for item in report.scenarios] == ["tool_call"]
    # 但 cases 仍保留全部，JSON 才是完整证据
    assert len(report.cases) == 3


def test_failures_property() -> None:
    """``failures`` 只挑未通过的用例。"""
    assert [item.id for item in sample_report().failures] == ["qa-002"]


# --------------------------------------------------------------------------- #
# to_markdown
# --------------------------------------------------------------------------- #


def test_markdown_declares_mode_in_header() -> None:
    """头部必须声明两种口径并给出不可比提示。"""
    text = sample_report().to_markdown()
    assert "| 生成方式 | `fake` |" in text
    assert "| 检索方式 | `vector` |" in text
    assert "不可直接比较" in text


def test_markdown_has_per_scenario_sections() -> None:
    """按场景分节，并列出各检查项通过率。"""
    text = sample_report().to_markdown()
    assert "## 场景：`knowledge_qa`" in text
    assert "## 场景：`tool_call`" in text
    assert "| `retrieval_hit@3` | 50.0% |" in text


def test_markdown_failure_row_carries_trace_id() -> None:
    """失败清单每行都要带 trace_id 与失败原因。"""
    text = sample_report().to_markdown()
    failure_line = next(line for line in text.splitlines() if "`qa-002`" in line)
    assert "`trace-qa-002`" in failure_line
    assert "retrieval_hit@3" in failure_line
    assert "命中 0/1" in failure_line


def test_markdown_failure_section_shows_runnable_command() -> None:
    """失败清单后附可直接执行的排查命令，且带 trace 目录。"""
    text = sample_report().to_markdown()
    assert "查看某条失败用例的 span 树" in text
    assert TRACE_VIEW_COMMAND.format(trace_id="<trace_id>", trace_dir="") in text
    assert "--dir logs/eval/traces" in text


def test_markdown_without_failures_says_so() -> None:
    """全通过时明说没有失败用例，而不是留一张空表。"""
    cases = [case("a", "tool_call")]
    report = build_report(
        cases,
        tag="t",
        dataset="d",
        dataset_size=1,
        chat_mode="fake",
        retrieval_mode="vector",
        trace_dir="logs/eval/traces",
    )
    text = report.to_markdown()
    assert "本次没有失败用例。" in text


def test_markdown_error_only_failure_renders() -> None:
    """只有异常没有指标时，原因列不能是空的。"""
    row = case("boom", "tool_call", passed=False, metrics=[], error="RuntimeError: 崩了")
    report = build_report(
        [row],
        tag="t",
        dataset="d",
        dataset_size=1,
        chat_mode="fake",
        retrieval_mode="vector",
        trace_dir="logs/eval/traces",
    )
    assert "error=RuntimeError: 崩了" in report.to_markdown()


def test_markdown_missing_trace_id_shows_dash() -> None:
    """没有 trace_id 的失败用例留「—」，不假装有证据。"""
    row = case("no-trace", "tool_call", passed=False, trace_id=None)
    report = build_report(
        [row],
        tag="t",
        dataset="d",
        dataset_size=1,
        chat_mode="fake",
        retrieval_mode="vector",
        trace_dir="logs/eval/traces",
    )
    failure_line = next(line for line in report.to_markdown().splitlines() if "no-trace" in line)
    assert failure_line.rstrip().endswith("— |")


def test_markdown_rubric_section_when_disabled() -> None:
    """未开 Judge 时明说未启用。"""
    assert "本次未启用 Rubric" in sample_report().to_markdown()


def test_markdown_rubric_section_reports_agreement() -> None:
    """开了 Judge 时给出众数分、一致率、解析率。"""
    report = sample_report()
    report.rubric = [
        RubricResult(answer_id="qa-001", scores=[4, 4, 4], mode_score=4, agreement=1.0, parsed=1.0),
        RubricResult(answer_id="qa-002", scores=[3, 5], mode_score=3, agreement=0.5, parsed=1.0),
    ]
    text = report.to_markdown()
    assert "| `qa-001` | 4 | 100% | 3 | 100% |" in text
    assert "`qa-002`" in text
    assert "一致率未达 100% 的用例" in text


def test_markdown_rubric_section_all_stable() -> None:
    """全部一致时给出正面结论。"""
    report = sample_report()
    report.rubric = [
        RubricResult(answer_id="qa-001", scores=[5, 5], mode_score=5, agreement=1.0, parsed=1.0)
    ]
    assert "所有用例的判定一致率为 100%。" in report.to_markdown()


def test_format_trace_hint_uses_report_trace_dir() -> None:
    """单条 trace 的排查命令用报告自己的 trace 目录。"""
    report = sample_report()
    hint = report.format_trace_hint("abc123")
    assert hint == TRACE_VIEW_COMMAND.format(trace_id="abc123", trace_dir="logs/eval/traces")


# --------------------------------------------------------------------------- #
# to_json
# --------------------------------------------------------------------------- #


def test_to_json_writes_utf8_and_round_trips(tmp_path) -> None:
    """JSON 是可再次读回的完整证据。"""
    report = sample_report()
    target = report.to_json(tmp_path / "nested" / "run.json")

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["tag"] == "dryrun"
    assert len(payload["cases"]) == 3
    # 指标明细与 trace_id 必须落盘，否则「为什么掉了」只能重跑
    failed = next(item for item in payload["cases"] if item["id"] == "qa-002")
    assert failed["trace_id"] == "trace-qa-002"
    assert failed["metrics"][0]["detail"] == "命中 0/1"
    # 中文不转义，便于直接 diff
    assert "场景" in json.dumps(payload["totals"], ensure_ascii=False) or payload["totals"]


# --------------------------------------------------------------------------- #
# rubric.parse_score
# --------------------------------------------------------------------------- #


def test_parse_score_plain_json() -> None:
    """标准 JSON 直接解析。"""
    assert parse_score('{"score": 4, "reason": "基本齐全"}') == (4, "基本齐全")


def test_parse_score_tolerates_code_fence() -> None:
    """模型爱用代码块包裹，必须容忍。"""
    text = '```json\n{"score": 5, "reason": "要点齐全"}\n```'
    assert parse_score(text) == (5, "要点齐全")


def test_parse_score_tolerates_surrounding_prose() -> None:
    """前后带解释文字也能抠出分数。"""
    text = '好的，我的评判如下：{"score": 2, "reason": "臆造"} 以上。'
    assert parse_score(text) == (2, "臆造")


def test_parse_score_bare_digit() -> None:
    """只回一个裸数字时退化解析。"""
    assert parse_score("3") == (3, "")


@pytest.mark.parametrize("text", ["0", "6", "分数是 4 分", "", "抱歉，我无法评分"])
def test_parse_score_returns_none_on_failure(text: str) -> None:
    """解析失败返回 None，绝不返回 0——否则「模型不听话」会伪装成「回答差」。"""
    assert parse_score(text) is None


def test_parse_score_without_reason() -> None:
    """只有分数没有理由时理由为空串。"""
    assert parse_score('{"score": 5}') == (5, "")


# --------------------------------------------------------------------------- #
# rubric 环境变量
# --------------------------------------------------------------------------- #


def test_resolve_judge_model_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """未设或空白时回退到传入的缺省。"""
    monkeypatch.delenv(JUDGE_MODEL_ENV, raising=False)
    assert resolve_judge_model("qwen3") == "qwen3"

    monkeypatch.setenv(JUDGE_MODEL_ENV, "   ")
    assert resolve_judge_model("qwen3") == "qwen3"

    monkeypatch.setenv(JUDGE_MODEL_ENV, " qwen2.5:3b ")
    assert resolve_judge_model("qwen3") == "qwen2.5:3b"


def test_resolve_repeats_default_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """未设或非法值回退缺省。"""
    monkeypatch.delenv(JUDGE_REPEATS_ENV, raising=False)
    assert resolve_repeats() == DEFAULT_REPEATS

    monkeypatch.setenv(JUDGE_REPEATS_ENV, "abc")
    assert resolve_repeats() == DEFAULT_REPEATS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", 1), ("-3", 1), ("1", 1), ("3", 3), ("9", 9), ("99", MAX_REPEATS)],
)
def test_resolve_repeats_clamped(monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
    """重复次数夹在 [1, MAX_REPEATS]，防止误配把本地模型跑爆。"""
    monkeypatch.setenv(JUDGE_REPEATS_ENV, raw)
    assert resolve_repeats() == expected


# --------------------------------------------------------------------------- #
# rubric.build_messages / judge_note / dumps
# --------------------------------------------------------------------------- #


def test_build_messages_routes_by_task_and_hides_candidates() -> None:
    """``task=rubric`` 供替身路由；提示词里不含候选答案，避免位置偏好。"""
    messages = build_messages("问题？", "回答。", reference="参考")
    assert messages[0]["task"] == "rubric"
    assert "评分锚点" in messages[0]["content"]
    assert "问题：问题？" in messages[1]["content"]
    assert "参考资料：参考" in messages[1]["content"]
    assert "待评回答：回答。" in messages[1]["content"]
    assert len(messages) == 2


def test_judge_note_declares_self_consistency_bias() -> None:
    """自洽偏差必须写进声明，不能只给分数。"""
    note = judge_note("qwen3")
    assert "qwen3" in note
    assert "自洽偏差" in note


def test_judge_note_without_model() -> None:
    """未指定判定模型时说明与主模型相同。"""
    assert "与主模型相同" in judge_note(None)


async def test_dumps_is_single_line_json() -> None:
    """压成一行，便于塞进报告。"""
    scorer = RubricScorer(constant_chat('{"score": 4, "reason": "ok"}'), repeats=1)
    scored = await scorer.score(answer_id="x", question="q", answer="a")
    text = dumps(scored)
    assert "\n" not in text
    assert json.loads(text)["mode_score"] == 4


# --------------------------------------------------------------------------- #
# RubricScorer
# --------------------------------------------------------------------------- #


async def test_scorer_majority_and_stability() -> None:
    """三次同分：众数即该分，一致率 100%，标记为可采信。"""
    scorer = RubricScorer(
        make_chat(['{"score": 4, "reason": "a"}'] * 3),
        model="fake-judge",
        repeats=3,
    )
    result = await scorer.score(answer_id="qa-001", question="q", answer="a")
    assert result.scores == [4, 4, 4]
    assert result.mode_score == 4
    assert result.agreement == pytest.approx(1.0)
    assert result.parsed == pytest.approx(1.0)
    assert result.model == "fake-judge"
    assert result.stable is True


async def test_scorer_agreement_below_one_is_unstable() -> None:
    """两次 4 分一次 5 分：众数 4，一致率 2/3，不可采信。"""
    scorer = RubricScorer(
        make_chat(
            [
                '{"score": 4, "reason": "a"}',
                '{"score": 5, "reason": "b"}',
                '{"score": 4, "reason": "c"}',
            ]
        ),
        repeats=3,
    )
    result = await scorer.score(answer_id="qa-002", question="q", answer="a")
    assert result.mode_score == 4
    assert result.agreement == pytest.approx(2 / 3)
    assert result.stable is False


async def test_scorer_tie_breaks_toward_lower_score() -> None:
    """并列时取较低分：偏保守，不让「判高判低各半」被记成高分。"""
    scorer = RubricScorer(
        make_chat(['{"score": 5, "reason": "a"}', '{"score": 3, "reason": "b"}']),
        repeats=2,
    )
    result = await scorer.score(answer_id="qa-003", question="q", answer="a")
    assert result.mode_score == 3
    assert result.agreement == pytest.approx(0.5)


async def test_scorer_unparsable_attempts_are_dropped_not_zeroed() -> None:
    """解析失败的次数不计入分数，只压低解析率。"""
    scorer = RubricScorer(
        make_chat(['{"score": 5, "reason": "a"}', "抱歉，我无法评分"]),
        repeats=2,
    )
    result = await scorer.score(answer_id="qa-004", question="q", answer="a")
    assert result.scores == [5]
    assert result.mode_score == 5
    assert result.agreement == pytest.approx(1.0)
    assert result.parsed == pytest.approx(0.5)


async def test_scorer_all_unparsable_yields_no_score() -> None:
    """全部解析失败时众数为 None 而不是 0。"""
    scorer = RubricScorer(constant_chat("我不会打分"), repeats=3)
    result = await scorer.score(answer_id="qa-005", question="q", answer="a")
    assert result.scores == []
    assert result.mode_score is None
    assert result.agreement == 0.0
    assert result.parsed == 0.0
    assert result.stable is False


async def test_scorer_clamps_repeats() -> None:
    """构造时就把重复次数夹住，属性如实反映生效值。"""
    assert RubricScorer(constant_chat("x"), repeats=0).repeats == 1
    assert RubricScorer(constant_chat("x"), repeats=99).repeats == MAX_REPEATS
    assert RubricScorer(constant_chat("x"), repeats=DEFAULT_REPEATS).repeats == DEFAULT_REPEATS


async def test_scorer_without_tracer_calls_chat_directly() -> None:
    """不传 tracer 时不产生 span，也不依赖可观测层可用（单测路径）。"""
    calls = {"n": 0}

    async def chat(messages):  # noqa: ANN001, ANN202, ARG001
        calls["n"] += 1
        return '{"score": 4, "reason": "ok"}'

    scorer = RubricScorer(chat, repeats=2)  # 不传 tracer
    await scorer.score(answer_id="x", question="q", answer="a")
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# Markdown 渲染
# --------------------------------------------------------------------------- #


def test_latency_in_markdown_is_readable() -> None:
    """延迟必须带单位、不得写成科学计数法。

    真实链路单条知识问答能跑到 10^5 毫秒量级，若沿用 ``4g`` 格式化会渲染成
    ``2.053e+05 ms``，读者还得自己换算——等于没给这个数。
    """
    report = build_report(
        [case("qa-001", "knowledge_qa", latency_ms=205_345.0)],
        tag="dryrun",
        dataset="data/golden/week10_golden.jsonl",
        dataset_size=1,
        chat_mode="real",
        retrieval_mode="real (qdrant + embedding)",
        trace_dir="logs/eval/traces",
    )
    text = report.to_markdown()

    assert "e+0" not in text, "延迟渲染成了科学计数法"
    assert "| latency_p50 |" in text
    assert "ms" in text


def test_failure_section_command_matches_configured_trace_dir() -> None:
    """失败清单里的排查命令要用报告自己的 trace 目录，别让人去猜路径。"""
    report = sample_report()
    text = report.to_markdown()

    expected = TRACE_VIEW_COMMAND.format(trace_id="<trace_id>", trace_dir="logs/eval/traces")

    assert expected in text
