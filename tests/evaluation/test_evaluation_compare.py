"""对比脚本的单元测试。

输入全是手搓的最小报告对象，不读真实产物：``logs/`` 是本地证据、不入库，
测试若依赖它就只能在跑过评测的机器上通过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import eval_week10_compare as cmp  # noqa: E402


def _case(
    case_id: str,
    passed: bool,
    metrics: list[dict[str, Any]],
    *,
    scenario: str = "knowledge_qa",
    group: str = "spec",
    latency_ms: float = 1000.0,
) -> dict[str, Any]:
    """造一条用例结果。"""
    return {
        "id": case_id,
        "scenario": scenario,
        "group": group,
        "passed": passed,
        "trace_id": f"trace-{case_id}",
        "latency_ms": latency_ms,
        "error": None,
        "metrics": metrics,
    }


def _metric(name: str, passed: bool, *, skipped: bool = False) -> dict[str, Any]:
    """造一个检查项结果。"""
    return {"name": name, "passed": passed, "value": None, "detail": "", "skipped": skipped}


def _report(
    tag: str,
    *,
    cases: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    latency_p95: float = 1000.0,
    path: str | None = None,
) -> dict[str, Any]:
    """造一份最小报告。"""
    report: dict[str, Any] = {
        "tag": tag,
        "dataset": "data/golden/week10_golden.jsonl",
        "dataset_size": 52,
        "chat_mode": "fake",
        "limit": None,
        "cases": cases,
        "totals": {"用例数": len(cases), "latency_p95": latency_p95, "total_tokens": 0.0},
        "scenarios": [
            {
                "scenario": "knowledge_qa",
                "total": len(cases),
                "passed": sum(1 for item in cases if item["passed"]),
                "metric_pass_rates": {"retrieval_hit@3": 0.5, "answer_point_coverage": 0.25},
            }
        ],
        "_path": path or f"logs/eval/week10_eval_{tag}.json",
    }
    if config is not None:
        report["config"] = config
    return report


def _full_config(**overrides: Any) -> dict[str, Any]:
    """一份完整的 config，便于逐项覆盖。"""
    config = {
        "retrieval_strategy": "vector",
        "prompt_variant": "default",
        "min_score": "用例自带",
        "chat_mode": "fake",
        "limit": "不限",
    }
    config.update(overrides)
    return config


def test_resolve_report_accepts_name_without_eval_infix(tmp_path: Path) -> None:
    """口语里写 week10_baseline.json，实际文件带 eval_ 中缀，应能对上。"""
    actual = tmp_path / "week10_eval_baseline.json"
    actual.write_text("{}", encoding="utf-8")

    assert cmp.resolve_report(tmp_path / "week10_baseline.json") == actual


def test_resolve_report_raises_with_both_paths_in_message(tmp_path: Path) -> None:
    """两种写法都找不到时，报错要把试过的路径都写出来。"""
    with pytest.raises(cmp.CompareError) as excinfo:
        cmp.resolve_report(tmp_path / "week10_missing.json")

    message = str(excinfo.value)
    assert "week10_missing.json" in message
    assert "week10_eval_missing.json" in message


def test_load_report_rejects_non_report(tmp_path: Path) -> None:
    """没有 cases 字段的 JSON 不算评测报告。"""
    path = tmp_path / "week10_eval_x.json"
    path.write_text(json.dumps({"tag": "x"}), encoding="utf-8")

    with pytest.raises(cmp.CompareError, match="缺少 cases"):
        cmp.load_report(path)


def test_load_report_reports_bad_json(tmp_path: Path) -> None:
    """坏 JSON 的报错要指向文件，而不是抛出裸的解析异常。"""
    path = tmp_path / "week10_eval_x.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(cmp.CompareError, match="不是合法 JSON"):
        cmp.load_report(path)


def test_label_falls_back_to_file_stem() -> None:
    """tag 缺失时退回文件名，至少让列有名字。"""
    report = _report("x", cases=[], path="logs/eval/week10_eval_fallback.json")
    report.pop("tag")

    assert cmp.label_of(report) == "week10_eval_fallback"


def test_changed_config_lists_real_differences() -> None:
    """只报真正变了的键，并且写明前后值。"""
    base = _report("baseline", cases=[], config=_full_config())
    variant = _report("retrieval", cases=[], config=_full_config(retrieval_strategy="hybrid"))

    assert cmp.changed_config(base, variant) == ["retrieval_strategy: vector → hybrid"]


def test_changed_config_is_empty_when_identical() -> None:
    """配置逐项相同时返回空列表——空列表的含义是「确认没有差异」。"""
    base = _report("baseline", cases=[], config=_full_config())
    same = _report("again", cases=[], config=_full_config())

    assert cmp.changed_config(base, same) == []


def test_changed_config_reports_missing_config_instead_of_no_diff() -> None:
    """缺 config 字段时必须说「核对不了」，不能退化成「没有差异」。"""
    base = _report("baseline", cases=[], config=None)
    variant = _report("retrieval", cases=[], config=_full_config())

    diffs = cmp.changed_config(base, variant)

    assert len(diffs) == 1
    assert "未记录 config" in diffs[0]
    assert "baseline" in diffs[0]


def test_config_missing_names_offenders() -> None:
    """列出没记录 config 的组名。"""
    with_config = _report("a", cases=[], config=_full_config())
    without = _report("b", cases=[])

    assert cmp.config_missing([with_config, without]) == ["b"]


def test_comparability_flags_case_set_and_limit_differences() -> None:
    """用例条数与用例集合的差异都属于「不可比」，要被抓出来。"""
    base = _report("baseline", cases=[_case("qa-001", True, [])])
    variant = _report("small", cases=[_case("qa-002", True, [])])
    variant["limit"] = 5

    issues = cmp.comparability_issues(base, variant)

    assert any("limit" in item for item in issues)
    assert any("仅基线有 1 条用例" in item for item in issues)
    assert any("仅变体有 1 条用例" in item for item in issues)


def test_comparability_clean_when_identical_shape() -> None:
    """形状一致时不该报任何口径问题。"""
    base = _report("baseline", cases=[_case("qa-001", True, [])])
    variant = _report("retrieval", cases=[_case("qa-001", True, [])])

    assert cmp.comparability_issues(base, variant) == []


def test_brief_truncates_long_id_lists() -> None:
    """长清单折成计数，避免把真正的告警淹掉。"""
    ids = [f"qa-{index:03d}" for index in range(20)]

    text = cmp._brief(ids)

    assert "等 20 条" in text
    assert "qa-019" not in text


def test_flip_rows_detects_case_level_regression() -> None:
    """整条用例 通过 → 失败 记为变差。"""
    metric = [_metric("retrieval_hit@3", True)]
    base = _report("baseline", cases=[_case("qa-001", True, metric)])
    variant = _report("retrieval", cases=[_case("qa-001", False, metric)])

    worse = cmp.flip_rows(base, variant, want="worse")

    assert [row["metric"] for row in worse] == ["（整条用例）"]
    assert worse[0]["before"] == "通过"
    assert worse[0]["after"] == "失败"


def test_flip_rows_detects_metric_level_regression_inside_failing_case() -> None:
    """用例整体仍失败、但某个检查项从通过变失败，同样是退化，不能漏。"""
    base = _report(
        "baseline",
        cases=[
            _case(
                "qa-001",
                False,
                [_metric("retrieval_hit@3", True), _metric("refusal_correct", False)],
            )
        ],
    )
    variant = _report(
        "retrieval",
        cases=[
            _case(
                "qa-001",
                False,
                [_metric("retrieval_hit@3", False), _metric("refusal_correct", False)],
            )
        ],
    )

    worse = cmp.flip_rows(base, variant, want="worse")

    assert [row["metric"] for row in worse] == ["retrieval_hit@3"]


def test_flip_rows_ignores_skipped_metrics() -> None:
    """跳过项不代表失败，不该计入翻转。"""
    base = _report(
        "baseline", cases=[_case("qa-001", True, [_metric("refusal_correct", False, skipped=True)])]
    )
    variant = _report(
        "retrieval", cases=[_case("qa-001", True, [_metric("refusal_correct", True)])]
    )

    assert cmp.flip_rows(base, variant, want="better") == []


def test_flip_rows_ignores_cases_present_on_one_side_only() -> None:
    """只在一侧出现的用例不构成翻转，它属于口径告警。"""
    base = _report("baseline", cases=[_case("qa-001", True, [])])
    variant = _report("retrieval", cases=[_case("qa-002", False, [])])

    assert cmp.flip_rows(base, variant, want="worse") == []


def test_flip_rows_reports_improvement() -> None:
    """失败 → 通过 记为变好。"""
    base = _report("baseline", cases=[_case("qa-001", False, [])])
    variant = _report("retrieval", cases=[_case("qa-001", True, [])])

    better = cmp.flip_rows(base, variant, want="better")

    assert better[0]["before"] == "失败"
    assert better[0]["after"] == "通过"


def test_stable_counts_splits_by_shared_state() -> None:
    """不变用例拆成两边都通过 / 两边都失败两类。"""
    base = _report(
        "baseline",
        cases=[_case("qa-001", True, []), _case("qa-002", False, []), _case("qa-003", True, [])],
    )
    variant = _report(
        "retrieval",
        cases=[_case("qa-001", True, []), _case("qa-002", False, []), _case("qa-003", False, [])],
    )

    assert cmp.stable_counts(base, variant) == (1, 1)


def test_dimension_reads_scenario_metric_and_totals() -> None:
    """固定维度分别落在场景指标与总体表里。"""
    report = _report("baseline", cases=[_case("qa-001", True, [])], latency_p95=2500.0)

    assert cmp.dimension_of(report, "retrieval_hit@3") == 0.5
    assert cmp.dimension_of(report, "latency_p95") == 2500.0
    assert cmp.dimension_of(report, "total_tokens") == 0.0
    assert cmp.dimension_of(report, "不存在的指标") is None


def test_latency_rows_group_by_scenario_with_median_and_total() -> None:
    """分场景给「单条中位 / 合计」：跨场景求分位数没有含义，得分开列。"""
    base = _report(
        "baseline",
        cases=[
            _case("qa-001", True, [], latency_ms=100_000.0),
            _case("qa-002", True, [], latency_ms=200_000.0),
            _case("req-001", True, [], scenario="requirement_analysis", latency_ms=200.0),
        ],
    )
    variant = _report(
        "retrieval",
        cases=[_case("qa-001", True, [], latency_ms=50_000.0)],
    )

    rows = cmp.latency_rows(base, [variant])

    assert [row[0] for row in rows] == ["`knowledge_qa`", "`requirement_analysis`"]
    # 两条问答用例的中位取中间两条的平均，合计是 300 秒 = 5.0 分钟
    assert rows[0][1] == "`150.0 s` / `5.0 min`"
    assert rows[0][2] == "`50.0 s` / `50.0 s`"
    # 变体组没有这个场景，给显式占位而不是 0
    assert rows[1][2] == "—"


def test_render_warns_that_overall_p50_is_not_the_qa_latency() -> None:
    """总体 p50 被替身节点拉到毫秒级，必须写明该看分场景表。"""
    base = _report(
        "baseline",
        cases=[_case("qa-001", True, [], latency_ms=120_000.0)],
        config=_full_config(),
    )
    variant = _report(
        "retrieval",
        cases=[_case("qa-001", True, [], latency_ms=110_000.0)],
        config=_full_config(retrieval_strategy="hybrid"),
    )

    text = cmp.render(base, [variant], generated_at="x")

    assert "### 按场景的延迟" in text
    assert "`120.0 s` / `2.0 min`" in text
    assert "看延迟一律以这张分场景表为准" in text


def test_render_includes_fixed_dimensions_and_regressions() -> None:
    """端到端：六个固定维度都在，变差用例被逐条列出。"""
    base = _report(
        "baseline",
        cases=[_case("qa-001", True, [_metric("retrieval_hit@3", True)])],
        config=_full_config(),
    )
    variant = _report(
        "retrieval",
        cases=[_case("qa-001", False, [_metric("retrieval_hit@3", False)])],
        config=_full_config(retrieval_strategy="hybrid"),
    )

    text = cmp.render(base, [variant], generated_at="2026-09-23T00:00:00+00:00")

    for name in cmp.DIMENSIONS:
        assert f"`{name}`" in text
    assert "`qa-001`" in text
    assert "retrieval_strategy: vector → hybrid" in text
    assert text.startswith(cmp.MARK_BEGIN)
    assert text.rstrip().endswith(cmp.MARK_END)


def test_render_warns_when_more_than_one_variable_changed() -> None:
    """一次改两个旋钮就说清不可归因，别让读者自己去数。"""
    base = _report("baseline", cases=[], config=_full_config())
    variant = _report(
        "both",
        cases=[],
        config=_full_config(retrieval_strategy="hybrid", prompt_variant="grounded"),
    )

    text = cmp.render(base, [variant], generated_at="x")

    assert "多变量对比" in text


def test_render_warns_when_token_usage_is_empty() -> None:
    """token 全 0 要说清是没采到，不是「没有额外成本」。"""
    base = _report("baseline", cases=[], config=_full_config())
    variant = _report("retrieval", cases=[], config=_full_config(retrieval_strategy="hybrid"))

    text = cmp.render(base, [variant], generated_at="x")

    assert "`total_tokens` 各列都是 0" in text


def test_render_marks_missing_config() -> None:
    """缺 config 的组要在告警里点名。"""
    base = _report("baseline", cases=[])
    variant = _report("retrieval", cases=[], config=_full_config(retrieval_strategy="hybrid"))

    text = cmp.render(base, [variant], generated_at="x")

    assert "没有 `config` 字段" in text


def test_write_out_creates_file_with_markers(tmp_path: Path) -> None:
    """目标不存在时整份写出，并带上标记供后续替换。"""
    target = tmp_path / "report.md"
    block = f"{cmp.MARK_BEGIN}\n内容\n{cmp.MARK_END}\n"

    assert cmp.write_out(target, block) == "新建"
    assert cmp.MARK_BEGIN in target.read_text(encoding="utf-8")


def test_write_out_splices_and_keeps_handwritten_text(tmp_path: Path) -> None:
    """已存在的文件只换数字块，手写叙述必须原样保留。"""
    target = tmp_path / "report.md"
    target.write_text(
        f"# 手写标题\n\n{cmp.MARK_BEGIN}\n旧内容\n{cmp.MARK_END}\n\n## 手写小结\n\n结论\n",
        encoding="utf-8",
    )

    assert cmp.write_out(target, f"{cmp.MARK_BEGIN}\n新内容\n{cmp.MARK_END}\n") == "替换生成块"
    text = target.read_text(encoding="utf-8")
    assert "新内容" in text
    assert "旧内容" not in text
    assert "# 手写标题" in text
    assert "## 手写小结" in text


def test_write_out_refuses_file_without_markers(tmp_path: Path) -> None:
    """没有标记就拒绝改写，避免把整份手写文档覆盖掉。"""
    target = tmp_path / "report.md"
    target.write_text("# 手写文档，没有标记\n", encoding="utf-8")

    with pytest.raises(cmp.CompareError, match="找不到生成标记"):
        cmp.write_out(target, f"{cmp.MARK_BEGIN}\n块\n{cmp.MARK_END}\n")


def test_write_out_keeps_blank_line_after_marker(tmp_path: Path) -> None:
    """生成块与后面的正文之间留一个空行，且重复替换不会把它吃掉。"""
    target = tmp_path / "report.md"
    target.write_text(
        f"# 标题\n\n{cmp.MARK_BEGIN}\n旧\n{cmp.MARK_END}\n\n### 3.1 手写小节\n\n正文\n",
        encoding="utf-8",
    )
    block = f"{cmp.MARK_BEGIN}\n新\n{cmp.MARK_END}\n"

    for _ in range(3):
        cmp.write_out(target, block)
        text = target.read_text(encoding="utf-8")
        assert f"{cmp.MARK_END}\n\n### 3.1 手写小节" in text, "标记行与下一节之间必须正好一个空行"


def test_parse_args_accepts_multiple_variants() -> None:
    """对比两组以上时 --variant 收多个路径。"""
    args = cmp.parse_args(
        ["--baseline", "b.json", "--variant", "r.json", "p.json", "--out", "o.md"]
    )

    assert args.baseline == "b.json"
    assert args.variant == ["r.json", "p.json"]
    assert args.out == "o.md"


def test_parse_args_requires_baseline_and_variant() -> None:
    """两个位置参数都必填，缺了直接报错而不是跑出半个对比。"""
    with pytest.raises(SystemExit):
        cmp.parse_args(["--baseline", "b.json"])


def test_main_creates_output_and_reports_flips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """命令行入口跑通：既写出文件，也把翻转数报到标准输出。"""
    base_path = tmp_path / "week10_eval_baseline.json"
    variant_path = tmp_path / "week10_eval_retrieval.json"
    out = tmp_path / "out.md"
    base = _report("baseline", cases=[_case("qa-001", True, [])], config=_full_config())
    variant = _report(
        "retrieval",
        cases=[_case("qa-001", False, [])],
        config=_full_config(retrieval_strategy="hybrid"),
    )
    base_path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    variant_path.write_text(json.dumps(variant, ensure_ascii=False), encoding="utf-8")

    code = cmp.main(
        ["--baseline", str(base_path), "--variant", str(variant_path), "--out", str(out)]
    )

    assert code == 0
    assert "变差 1 项" in capsys.readouterr().out
    assert "`qa-001`" in out.read_text(encoding="utf-8")


def test_main_reports_missing_file_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """找不到文件时给出可读的失败信息并返回非零，而不是抛栈。"""
    code = cmp.main(
        [
            "--baseline",
            str(tmp_path / "nope.json"),
            "--variant",
            str(tmp_path / "also-nope.json"),
            "--out",
            str(tmp_path / "out.md"),
        ]
    )

    assert code == 1
    assert "[FAIL]" in capsys.readouterr().err
