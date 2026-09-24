"""评测报告：一份机器可读 JSON、一份人类可读 Markdown。

为什么是两份
------------

- **JSON** 给后续的对比实验用（第 4 天要按 tag 拉两组结果做差异比对）。它必须
  自带每条用例的 ``checks`` 明细与 ``trace_id``，否则「这个指标为什么掉了」
  只能靠重跑。
- **Markdown** 给人看。按场景分节，先给指标汇总表，再给失败清单。失败清单里
  **每条都带 trace_id**，并附一条可以直接执行的查看命令——报告的价值不在于
  「有 12 条没过」，而在于「点开即能看到那一条的 span 树」。

口径声明
--------

报告头部固定声明本次运行的 ``chat_mode`` 与 ``retrieval_mode``。同一份数据集在
「真实模型」与「确定性替身」下跑出来的数字不可比，不写清楚就会被误读成回归。
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .metrics import MetricResult, percentile_from_buckets

TRACE_VIEW_COMMAND = (
    "uv run python scripts/trace_week10_view.py --trace-id {trace_id} --dir {trace_dir}"
)
"""失败用例对应的排查命令模板。"""


class CaseResult(BaseModel):
    """一条用例的执行结果。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    scenario: str
    group: str
    passed: bool
    trace_id: str | None = None
    latency_ms: float | None = None
    metrics: list[MetricResult] = Field(default_factory=list)
    error: str | None = None

    @property
    def failed_metrics(self) -> list[MetricResult]:
        """未通过且未被跳过的检查项。"""
        return [item for item in self.metrics if not item.passed and not item.skipped]

    @property
    def skipped_metrics(self) -> list[MetricResult]:
        """被跳过的检查项。"""
        return [item for item in self.metrics if item.skipped]


class ScenarioSummary(BaseModel):
    """一个场景的汇总。"""

    model_config = ConfigDict(extra="forbid")

    scenario: str
    total: int
    passed: int
    failed: int
    skipped_metrics: int
    pass_rate: float
    metric_pass_rates: dict[str, float] = Field(default_factory=dict)
    latency_p50: float | None = None
    latency_p95: float | None = None


class EvalReport(BaseModel):
    """一次评测的完整结果。"""

    model_config = ConfigDict(extra="forbid")

    tag: str
    dataset: str
    dataset_size: int
    generated_at: str
    chat_mode: str
    retrieval_mode: str
    trace_dir: str
    config: dict[str, Any] = Field(default_factory=dict)
    """本次执行的可变参数（检索策略、提示词变体、拒答阈值等）。

    对比脚本按这份记录给各列贴标签，也用它判断两组之间到底改了什么——只靠
    ``tag`` 命名区分，读的人无从确认差异是否真的只来自那一个变量。
    """

    scenarios: list[ScenarioSummary] = Field(default_factory=list)
    cases: list[CaseResult] = Field(default_factory=list)
    rubric: list[Any] = Field(default_factory=list)
    totals: dict[str, float] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def failures(self) -> list[CaseResult]:
        """未通过的用例。"""
        return [item for item in self.cases if not item.passed]

    def to_json(self, path: str | Path) -> Path:
        """写机器可读结果。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target

    def to_markdown(self) -> str:
        """渲染人类可读报告。"""
        lines: list[str] = [
            f"# 第 10 周评测报告（tag={self.tag}）",
            "",
            "| 项 | 值 |",
            "| --- | --- |",
            f"| 数据集 | `{self.dataset}`（{self.dataset_size} 条） |",
            f"| 生成时间 | {self.generated_at} |",
            f"| 生成方式 | `{self.chat_mode}` |",
            f"| 检索方式 | `{self.retrieval_mode}` |",
            f"| trace 目录 | `{self.trace_dir}` |",
            "",
            "> 口径说明：同一份数据集在不同 `chat_mode` / `retrieval_mode` 下跑出的数字不可直接比较。",
            "",
        ]
        if self.config:
            lines.extend(["| 本次参数 | 值 |", "| --- | --- |"])
            for key, value in self.config.items():
                lines.append(f"| {key} | `{value}` |")
            lines.append("")
        if self.notes:
            lines.append("> " + "；".join(self.notes))
            lines.append("")

        lines.extend(
            [
                "## 总体",
                "",
                "| 指标 | 值 |",
                "| --- | --- |",
            ]
        )
        for key, value in self.totals.items():
            lines.append(
                f"| {key} | {_fmt_ms(value) if key.startswith('latency_') else _fmt(value)} |"
            )
        lines.append("")

        for summary in self.scenarios:
            lines.extend(_scenario_section(summary))
        lines.extend(self._rubric_section())
        lines.extend(self._failure_section())
        return "\n".join(lines) + "\n"

    def _rubric_section(self) -> list[str]:
        """Rubric 一致率。判定不稳定时分数没有解释力，所以先看一致率再看分。"""
        lines = ["## Rubric（LLM-as-a-Judge）", ""]
        if not self.rubric:
            lines.extend(["本次未启用 Rubric（``--judge off``）。", ""])
            return lines
        lines.extend(
            [
                "| 用例 | 众数分 | 一致率 | 判定次数 | 成功解析 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in self.rubric:
            lines.append(
                f"| `{item.answer_id}` | {item.mode_score if item.mode_score is not None else '—'} "
                f"| {item.agreement:.0%} | {len(item.scores)} | {item.parsed:.0%} |"
            )
        unstable = [item.answer_id for item in self.rubric if not item.stable]
        lines.append("")
        if unstable:
            lines.append(f"一致率未达 100% 的用例（分数仅供参考，建议人工复核）：{unstable}")
        else:
            lines.append("所有用例的判定一致率为 100%。")
        lines.append("")
        return lines

    def _failure_section(self) -> list[str]:
        lines = ["## 失败用例", ""]
        failures = self.failures
        if not failures:
            lines.extend(["本次没有失败用例。", ""])
            return lines
        lines.extend(
            [
                "| 用例 | 场景 | 分组 | 未通过的检查项 | trace_id |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in failures:
            reasons = "; ".join(metric.render() for metric in item.failed_metrics)
            if item.error:
                reasons = f"{reasons}; error={item.error}" if reasons else f"error={item.error}"
            trace = f"`{item.trace_id}`" if item.trace_id else "—"
            lines.append(
                f"| `{item.id}` | {item.scenario} | {item.group} | {reasons or '—'} | {trace} |"
            )
        lines.extend(["", "查看某条失败用例的 span 树：", "", "```bash"])
        lines.append(TRACE_VIEW_COMMAND.format(trace_id="<trace_id>", trace_dir=self.trace_dir))
        lines.append("```")
        lines.append("")
        return lines

    def format_trace_hint(self, trace_id: str) -> str:
        """给单条 trace 生成排查命令。"""
        return TRACE_VIEW_COMMAND.format(trace_id=trace_id, trace_dir=self.trace_dir)


def _fmt(value: float | None) -> str:
    """数值格式化：整数不带小数，浮点保留到 4 位有效数字。"""
    if value is None:
        return "—"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.4g}"


def _fmt_ms(value: float | None) -> str:
    """延迟格式化：一律带单位。

    延迟可达 10^5 毫秒量级，若沿用 :func:`_fmt` 的 4 位有效数字会写成
    ``2.053e+05``，读者还得自己换算；超过 10 秒时补一个秒数，省掉数位。
    """
    if value is None:
        return "—"
    ms = float(value)
    if ms >= 10_000:
        return f"{ms:,.0f} ms（{ms / 1000:.1f} s）"
    return f"{ms:,.0f} ms"


def _scenario_section(summary: ScenarioSummary) -> list[str]:
    lines = [
        f"## 场景：`{summary.scenario}`",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 用例数 | {summary.total} |",
        f"| 通过 | {summary.passed} |",
        f"| 失败 | {summary.failed} |",
        f"| 通过率 | {summary.pass_rate:.1%} |",
        f"| latency_p50 | {_fmt_ms(summary.latency_p50)} |",
        f"| latency_p95 | {_fmt_ms(summary.latency_p95)} |",
        "",
    ]
    if summary.metric_pass_rates:
        lines.extend(
            [
                "各检查项通过率（分母不含被跳过的判定）：",
                "",
                "| 检查项 | 通过率 |",
                "| --- | --- |",
            ]
        )
        for name, rate in sorted(summary.metric_pass_rates.items()):
            lines.append(f"| `{name}` | {rate:.1%} |")
        lines.append("")
    return lines


def summarize(
    cases: list[CaseResult],
    *,
    scenario: str | None = None,
) -> list[ScenarioSummary]:
    """按场景汇总。``scenario`` 非空时只汇总该场景。"""
    grouped: dict[str, list[CaseResult]] = defaultdict(list)
    for item in cases:
        grouped[item.scenario].append(item)

    summaries: list[ScenarioSummary] = []
    for name in sorted(grouped):
        if scenario is not None and name != scenario:
            continue
        rows = grouped[name]
        passed = sum(1 for row in rows if row.passed)
        latencies = [row.latency_ms for row in rows if row.latency_ms is not None]

        buckets: dict[str, list[bool]] = defaultdict(list)
        skipped = 0
        for row in rows:
            for metric in row.metrics:
                if metric.skipped:
                    skipped += 1
                    continue
                buckets[metric.name].append(metric.passed)

        summaries.append(
            ScenarioSummary(
                scenario=name,
                total=len(rows),
                passed=passed,
                failed=len(rows) - passed,
                skipped_metrics=skipped,
                pass_rate=(passed / len(rows)) if rows else 0.0,
                metric_pass_rates={
                    key: sum(1 for flag in flags if flag) / len(flags)
                    for key, flags in buckets.items()
                    if flags
                },
                latency_p50=percentile_from_buckets(latencies, 0.5),
                latency_p95=percentile_from_buckets(latencies, 0.95),
            )
        )
    return summaries


def build_report(
    cases: list[CaseResult],
    *,
    tag: str,
    dataset: str,
    dataset_size: int,
    chat_mode: str,
    retrieval_mode: str,
    trace_dir: str,
    scenario: str | None = None,
    notes: list[str] | None = None,
    usage: dict[str, float] | None = None,
    config: dict[str, Any] | None = None,
) -> EvalReport:
    """组装报告对象。"""
    totals: dict[str, Any] = {
        "用例数": len(cases),
        "通过": sum(1 for item in cases if item.passed),
        "失败": sum(1 for item in cases if not item.passed),
        "pass_rate": (sum(1 for item in cases if item.passed) / len(cases)) if cases else 0.0,
    }
    latencies = [item.latency_ms for item in cases if item.latency_ms is not None]
    totals["latency_p50"] = percentile_from_buckets(latencies, 0.5)
    totals["latency_p95"] = percentile_from_buckets(latencies, 0.95)
    if usage:
        totals["total_tokens"] = usage.get("total_tokens", 0.0)
        totals["cost_usd"] = usage.get("cost_usd", 0.0)
    return EvalReport(
        tag=tag,
        dataset=dataset,
        dataset_size=dataset_size,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        chat_mode=chat_mode,
        retrieval_mode=retrieval_mode,
        trace_dir=trace_dir,
        config=dict(config or {}),
        scenarios=summarize(cases, scenario=scenario),
        cases=cases,
        totals=totals,
        notes=list(notes or []),
    )


__all__ = [
    "TRACE_VIEW_COMMAND",
    "CaseResult",
    "EvalReport",
    "ScenarioSummary",
    "build_report",
    "summarize",
]
