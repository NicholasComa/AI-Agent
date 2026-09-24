"""把多组评测结果并排成一张对比表，并逐条列出变差/变好的用例。

用法（Git Bash，先 ``export PATH="/c/Users/Xsz/.local/bin:$PATH"``，在项目根执行）::

    uv run python scripts/eval_week10_compare.py --baseline logs/eval/week10_eval_baseline.json --variant logs/eval/week10_eval_retrieval.json logs/eval/week10_eval_prompt.json --out docs/week10_eval_report.md

每组的 ``.json`` 由 ``scripts/eval_week10_run.py`` 产出。只给一个 ``--variant``
也可以，两组同样能比。

为什么要有这个脚本
------------------

只看总体通过率会掩盖退化：改动让 3 条本来通过的用例挂掉、同时又让另外 3 条本来
挂掉的通过，总数一模一样，报告里什么也看不出来。所以这里做两件事——按**固定维度**
并排数字，并**逐条**列出通过状态发生翻转的用例。前者回答「变没变」，后者回答
「变在哪」。

变量归因不靠 ``tag`` 命名
--------------------------

各列标签取自报告 JSON 里的 ``config`` 字段，并自动算出「相对基线改了什么」。仅凭
文件名（``week10_eval_prompt.json``）判断差异是靠不住的——命名是人写的，配置是程序
写的。若两组的 ``config`` 差出不止一处，脚本会在报告顶部声明「非单变量」，因为那样
的数字无法归因到某一个旋钮。

口径可比性
----------

两组的数据集、用例条数、``chat_mode`` 与 ``limit`` 必须一致，否则数字不可比；不一致
时脚本仍会出表，但在最前面用 blockquote 明确标出差异项，避免被当成有效对比读。

输出位置
--------

``--out`` 指向的文件若已存在且带有生成标记，则只替换标记之间的内容、保留其余文字，
因此可以把它指向一份手写了叙述的文档（如 ``docs/week10_eval_report.md``），重复执行
只刷新数字块。文件不存在时按整份生成。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))  # noqa: E402

MARK_BEGIN = "<!-- BEGIN generated: eval_week10_compare.py -->"
MARK_END = "<!-- END generated: eval_week10_compare.py -->"

DEFAULT_OUT = PROJECT_ROOT / "logs" / "eval" / "week10_compare.md"

DIMENSIONS: tuple[str, ...] = (
    "retrieval_hit@3",
    "answer_point_coverage",
    "refusal_correct",
    "latency_p50",
    "latency_p95",
    "total_tokens",
)
"""固定对照维度。

前三个是 ``knowledge_qa`` 场景的检查项通过率，后三个来自总体表。维度写死而不是
"有什么指标就比什么"，是为了让每组的表长得一样——只有形状稳定，跨组读数才不至于
看漏一列。
"""

#定义一个元组常量，列出所有配置项的名字
CONFIG_KEYS: tuple[str, ...] = (
    "retrieval_strategy",
    "prompt_variant",
    "min_score",
    "chat_mode",
    "limit",
)

COMPARABILITY_KEYS: tuple[str, ...] = ("dataset", "dataset_size", "chat_mode", "limit")


class CompareError(Exception):
    """输入文件不合用时抛出。"""


def resolve_report(path: str | Path) -> Path:
    """定位报告文件；允许省略文件名里的 ``eval_`` 中缀。

    ``--tag baseline`` 产出的是 ``week10_eval_baseline.json``，而口语里常常写成
    ``week10_baseline.json``。这里做一次等价替换，省得因为文件名的中缀对不上就报
    "找不到文件"——那种失败与实验本身无关。
    """
    target = Path(path)
    if target.exists():
        return target
    alternative = target.with_name(target.name.replace("week10_", "week10_eval_", 1))
    if alternative.exists():
        return alternative
    raise CompareError(f"找不到报告文件：{target}（也试过 {alternative}）")


def load_report(path: str | Path) -> dict[str, Any]:
    """读取一份评测报告。"""
    resolved = resolve_report(path)
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CompareError(f"{resolved} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or "cases" not in data:
        raise CompareError(f"{resolved} 缺少 cases 字段，不像是评测报告")
    data["_path"] = resolved.as_posix() #把 Path 对象转成"POSIX风格"的字符串路径（用 / 分隔）
    return data


def label_of(report: dict[str, Any]) -> str:
    """列标签：优先用 tag，缺失时退回文件名。"""
    tag = str(report.get("tag") or "").strip()
    if tag:
        return tag
    return Path(str(report.get("_path") or "unknown")).stem


def config_of(report: dict[str, Any]) -> dict[str, Any]:
    """取本次运行的可变参数。"""
    raw = report.get("config")
    return dict(raw) if isinstance(raw, dict) else {}


def changed_config(baseline: dict[str, Any], variant: dict[str, Any]) -> list[str]:
    """列出变体相对基线真正变了的配置项。

    只比 ``CONFIG_KEYS``：数据集与条数属于可比性检查，不是实验变量。任一侧没有
    ``config`` 字段（例如报告生成于该字段引入之前）时返回一条说明，而不是返回空
    列表——空列表在本脚本里的含义是「确认没有差异」，两者不能混为一谈。
    """
    base, other = config_of(baseline), config_of(variant)
    missing = [
        label_of(report) for report, config in ((baseline, base), (variant, other)) if not config
    ]
    if missing:
        return [f"（{'、'.join(missing)} 未记录 config，改动无法逐项核对）"]
    diffs: list[str] = []
    for key in CONFIG_KEYS:
        before, after = base.get(key), other.get(key)
        if before != after:
            diffs.append(f"{key}: {before} → {after}")
    return diffs


def config_missing(reports: list[dict[str, Any]]) -> list[str]:
    """列出没有记录 ``config`` 的组。"""
    return [label_of(item) for item in reports if not config_of(item)]


MAX_LISTED_IDS = 8
"""口径告警里最多列出几个用例 id，超出部分折成计数。

一次只跑了一部分用例时，缺失清单能有几十条，全列出来会把真正的告警淹没。
"""


def _brief(ids: list[str]) -> str:
    """截断过长的 id 清单。"""
    if len(ids) <= MAX_LISTED_IDS:
        return ", ".join(ids)
    return f"{', '.join(ids[:MAX_LISTED_IDS])} 等 {len(ids)} 条"


def comparability_issues(baseline: dict[str, Any], variant: dict[str, Any]) -> list[str]:
    """找出会让两组数字不可比的差异。"""
    issues: list[str] = []
    for key in COMPARABILITY_KEYS:
        before, after = baseline.get(key), variant.get(key)
        if before != after:
            issues.append(f"{key}: {before} → {after}")
    base_ids = {str(item.get("id")) for item in baseline.get("cases", [])}
    other_ids = {str(item.get("id")) for item in variant.get("cases", [])}
    if base_ids != other_ids:
        only_base = sorted(base_ids - other_ids)
        only_other = sorted(other_ids - base_ids)
        if only_base:
            issues.append(f"仅基线有 {len(only_base)} 条用例：{_brief(only_base)}")
        if only_other:
            issues.append(f"仅变体有 {len(only_other)} 条用例：{_brief(only_other)}")
    return issues


def _case_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """按用例 id 索引。"""
    return {str(item.get("id")): item for item in report.get("cases", [])}


def stable_counts(baseline: dict[str, Any], variant: dict[str, Any]) -> tuple[int, int]:
    """返回 ``(两边都通过, 两边都失败)`` 的用例数。

    只看两组共有的用例。周计划要求差异明细覆盖「变好 / 变差 / 不变」三类，前两类
    逐条列出，不变的数量用这两个数说清即可——逐条列出几十条无变化的用例没有信息量。
    """
    base_cases, other_cases = _case_map(baseline), _case_map(variant)
    both_pass = both_fail = 0
    for case_id in base_cases.keys() & other_cases.keys():
        before = bool(base_cases[case_id].get("passed"))
        after = bool(other_cases[case_id].get("passed"))
        if before and after:
            both_pass += 1
        elif not before and not after:
            both_fail += 1
    return both_pass, both_fail


def _metric_of(case: dict[str, Any], name: str) -> dict[str, Any] | None:
    """取某条用例上某个检查项的结果。"""
    for item in case.get("metrics", []):
        if str(item.get("name")) == name:
            return item
    return None


def flip_rows(
    baseline: dict[str, Any], variant: dict[str, Any], *, want: str
) -> list[dict[str, Any]]:
    """列出通过状态发生翻转的用例。

    Args:
        want: ``"worse"`` 取 通过 → 未通过，``"better"`` 取 未通过 → 通过。

    除了整条用例的翻转，还会带上**检查项级别**的翻转：用例整体仍然是失败，但某个
    检查项从通过变成失败，同样是退化，只按用例看会漏掉。
    """
    base_cases, other_cases = _case_map(baseline), _case_map(variant)
    rows: list[dict[str, Any]] = []
    for case_id in sorted(base_cases.keys() & other_cases.keys()):
        before, after = base_cases[case_id], other_cases[case_id]
        before_pass, after_pass = bool(before.get("passed")), bool(after.get("passed"))
        case_flip = after_pass if want == "better" else before_pass
        if case_flip and before_pass != after_pass:
            rows.append(
                {
                    "id": case_id,
                    "scenario": after.get("scenario") or before.get("scenario"),
                    "group": after.get("group") or before.get("group"),
                    "metric": "（整条用例）",
                    "before": "通过" if before_pass else "失败",
                    "after": "通过" if after_pass else "失败",
                }
            )
        for item in after.get("metrics", []):
            name = str(item.get("name"))
            if item.get("skipped"):
                continue
            old = _metric_of(before, name)
            if old is None or old.get("skipped"):
                continue
            was, now = bool(old.get("passed")), bool(item.get("passed"))
            if was == now:
                continue
            if (now and want == "better") or (was and want == "worse"):
                rows.append(
                    {
                        "id": case_id,
                        "scenario": after.get("scenario"),
                        "group": after.get("group"),
                        "metric": name,
                        "before": "通过" if was else "失败",
                        "after": "通过" if now else "失败",
                    }
                )
    return rows


def _fmt_rate(value: Any) -> str:
    """百分比格式化；取不到值时给出显式占位而不是 0。"""
    if value is None:
        return "—"
    return f"{float(value) * 100:.1f}%"


def _fmt_ms(value: Any) -> str:
    """毫秒格式化：整数不带小数，大数补上秒。"""
    if value is None:
        return "—"
    number = float(value)
    if number >= 1000:
        return f"{number:,.0f} ms（{number / 1000:.1f} s）"
    if number.is_integer():
        return f"{int(number)} ms"
    return f"{number:.4g} ms"


def _fmt_number(value: Any) -> str:
    """普通数值。"""
    if value is None:
        return "—"
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.4g}"


def dimension_of(report: dict[str, Any], name: str) -> Any:
    """从报告里取某个固定维度的值。"""
    if name.startswith("latency_"):
        return report.get("totals", {}).get(name)
    if name == "total_tokens":
        return report.get("totals", {}).get(name)
    for summary in report.get("scenarios", []):
        rates = summary.get("metric_pass_rates") or {}
        if name in rates:
            return rates[name]
    return None


def scenario_metric_rows(report: dict[str, Any]) -> dict[tuple[str, str], float]:
    """摊平成 ``(场景, 指标) -> 通过率``。"""
    out: dict[tuple[str, str], float] = {}
    for summary in report.get("scenarios", []):
        scenario = str(summary.get("scenario"))
        for name, rate in (summary.get("metric_pass_rates") or {}).items():
            out[(scenario, name)] = float(rate)
    return out


def _fmt_sec(value: float) -> str:
    """毫秒转成带一位小数的秒。"""
    return f"{value / 1000:.1f} s"


def _fmt_span(value: float) -> str:
    """累计耗时：短于一分半用秒，再长就换成分钟。"""
    seconds = value / 1000
    if seconds < 90:
        return f"{seconds:.1f} s"
    return f"{seconds / 60:.1f} min"


def latency_rows(baseline: dict[str, Any], variants: list[dict[str, Any]]) -> list[list[str]]:
    """按场景汇总延迟。

    总表里的 ``latency_p50`` 会被两类替身节点用例拉低——它们只跑进程内直调图，
    耗时在毫秒级，和问答链路差三个数量级。分位数跨场景没有含义，所以这里按场景
    各给一份，每格是「单条中位 / 该场景合计」，合计代表这一轮的实际墙钟成本。
    """
    reports = [baseline, *variants]
    scenarios: list[str] = []
    for report in reports:
        for case in report.get("cases", []):
            name = str(case.get("scenario", ""))
            if name and name not in scenarios:
                scenarios.append(name)
    scenarios.sort()

    rows: list[list[str]] = []
    for scenario in scenarios:
        cells: list[str] = []
        for report in reports:
            values = [
                float(case.get("latency_ms") or 0.0)
                for case in report.get("cases", [])
                if str(case.get("scenario", "")) == scenario
            ]
            if not values:
                cells.append("—")
                continue
            values.sort()
            middle = (
                values[len(values) // 2]
                if len(values) % 2
                else (values[len(values) // 2 - 1] + values[len(values) // 2]) / 2
            )
            cells.append(f"`{_fmt_sec(middle)}` / `{_fmt_span(sum(values))}`")
        rows.append([f"`{scenario}`", *cells])
    return rows


def _fmt_dimension(report: dict[str, Any], name: str) -> str:
    """按维度类型选格式化方式。"""
    value = dimension_of(report, name)
    if name.startswith("latency_"):
        return _fmt_ms(value)
    if name == "total_tokens":
        return _fmt_number(value)
    return _fmt_rate(value)


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """渲染一张 Markdown 表。"""
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return lines


def _flip_table(rows: list[dict[str, Any]]) -> list[str]:
    """渲染翻转明细表。"""
    return _table(
        ["用例", "场景", "组", "检查项", "基线", "本组"],
        [
            [
                f"`{row['id']}`",
                f"`{row['scenario']}`",
                row["group"] or "—",
                f"`{row['metric']}`",
                row["before"],
                row["after"],
            ]
            for row in rows
        ],
    )


def render(
    baseline: dict[str, Any],
    variants: list[dict[str, Any]],
    *,
    generated_at: str,
) -> str:
    """渲染生成块。"""
    groups = [baseline, *variants]
    labels = [label_of(item) for item in groups]

    lines: list[str] = [
        MARK_BEGIN,
        "",
        "### 各组参数与相对基线的差异",
        "",
        f"生成时间：{generated_at}",
        "",
    ]
    rows: list[list[str]] = [
        ["报告文件", *[f"`{item.get('_path')}`" for item in groups]],
        ["tag", *[f"`{label_of(item)}`" for item in groups]],
    ]
    for key in CONFIG_KEYS:
        rows.append([f"`{key}`", *[f"`{config_of(item).get(key, '—')}`" for item in groups]])
    rows.append(["用例条数", *[str(len(item.get("cases", []))) for item in groups]])
    rows.append(["数据集", *[f"`{item.get('dataset')}`" for item in groups]])
    rows.append(
        [
            "相对基线的改动",
            "—",
            *["；".join(changed_config(baseline, item)) or "（无配置差异）" for item in variants],
        ]
    )
    lines.extend(_table(["项", *labels], rows))
    lines.append("")

    multi = [item for item in variants if len(changed_config(baseline, item)) > 1]
    if multi:
        names = "、".join(f"`{label_of(item)}`" for item in multi)
        lines.append(
            f"> 注意：{names} 相对基线改动了不止一个配置项，属于多变量对比，"
            "数字无法归因到单个旋钮。"
        )
        lines.append("")

    missing = config_missing(groups)
    if missing:
        names = "、".join(f"`{item}`" for item in missing)
        lines.append(
            f"> 口径告警：{names} 的报告里没有 `config` 字段（生成于该字段引入之前），"
            "因此「相对基线的改动」只能依据运行命令与命名推断，脚本无法核对。"
            "要消除这条告警，用同一版脚本重跑该组。"
        )
        lines.append("")

    issues_by_group: list[tuple[str, list[str]]] = []
    for item in variants:
        found = comparability_issues(baseline, item)
        if found:
            issues_by_group.append((label_of(item), found))
    if issues_by_group:
        lines.append("> 口径告警：以下差异会让两组数字不可直接比较。")
        for name, found in issues_by_group:
            lines.append(f"> - `{name}`：{'；'.join(found)}")
        lines.append("")

    lines.extend(["### 固定对照维度", ""])
    lines.extend(
        _table(
            ["维度", *labels],
            [
                [f"`{name}`", *[_fmt_dimension(item, name) for item in groups]]
                for name in DIMENSIONS
            ],
        )
    )
    lines.append("")

    tokens = [dimension_of(item, "total_tokens") for item in groups]
    if all(not value for value in tokens):
        lines.append(
            "> `total_tokens` 各列都是 0，说明本轮没有采集到用量：span 上的 `usage` "
            "始终为空，聚合结果自然是 0。该维度本组不可用，不要当作「改动没有增加成本」读。"
        )
        lines.append("")

    lines.extend(["### 总体", ""])
    total_metrics = ["用例数", "通过", "失败", "pass_rate", "latency_p50", "latency_p95"]
    lines.extend(
        _table(
            ["指标", *labels],
            [
                [
                    key,
                    *[
                        (
                            _fmt_rate(item.get("totals", {}).get(key))
                            if key == "pass_rate"
                            else _fmt_ms(item.get("totals", {}).get(key))
                            if key.startswith("latency_")
                            else _fmt_number(item.get("totals", {}).get(key))
                        )
                        for item in groups
                    ],
                ]
                for key in total_metrics
            ],
        )
    )
    lines.append("")

    lines.extend(["### 按场景 × 指标（通过率）", ""])
    per_group = [scenario_metric_rows(item) for item in groups]
    keys: list[tuple[str, str]] = []
    for mapping in per_group:
        for key in mapping:
            if key not in keys:
                keys.append(key)
    keys.sort()
    lines.extend(
        _table(
            ["场景", "指标", *labels],
            [
                [
                    f"`{scenario}`",
                    f"`{name}`",
                    *[
                        _fmt_rate(mapping.get((scenario, name)))
                        if (scenario, name) in mapping
                        else "—"
                        for mapping in per_group
                    ],
                ]
                for scenario, name in keys
            ],
        )
    )
    lines.append("")

    lines.extend(["### 按场景的延迟（每格：单条中位 / 该场景合计）", ""])
    rows = latency_rows(baseline, variants)
    if rows:
        lines.extend(_table(["场景", *labels], rows))
        lines.append("")
        lines.append(
            "> 上面「总体」里的 `latency_p50` 被 `requirement_analysis` 与 `tool_call` "
            "拉到了毫秒级：这两类用例走的是进程内直调图，节点用替身，和真实问答链路差"
            "三个数量级。看延迟一律以这张分场景表为准，合计按单条串行累加，代表一轮的"
            "实际墙钟成本。"
        )
        lines.append("")

    lines.extend(["### 逐条用例翻转", ""])
    for item in variants:
        name = label_of(item)
        worse = flip_rows(baseline, item, want="worse")
        better = flip_rows(baseline, item, want="better")
        both_pass, both_fail = stable_counts(baseline, item)
        lines.extend(
            [
                f"#### `{name}`",
                "",
                f"翻转 {len(worse) + len(better)} 项（变差 {len(worse)}、变好 {len(better)}）；"
                f"不变 {both_pass + both_fail} 条：两边都通过 {both_pass} 条、"
                f"两边都失败 {both_fail} 条。",
                "",
                "**变差**（改动引入的退化，逐条列出）",
                "",
            ]
        )
        lines.extend(_flip_table(worse) if worse else ["无变差用例。"])
        lines.extend(["", "**变好**", ""])
        lines.extend(_flip_table(better) if better else ["无变好用例。"])
        lines.append("")

    lines.append(MARK_END)
    lines.append("")
    return "\n".join(lines)


def splice(existing: str, block: str) -> str:
    """把手写文档里的生成块换掉，其余文字原样保留。

    生成块与后面的正文之间**固定留一个空行**：Markdown 里紧贴标题虽也能渲染，
    但「标记行下面直接跟 `###`」读起来像标记吞掉了正文的分隔，且每次重跑都会
    再吃掉一次。
    """
    if MARK_BEGIN not in existing or MARK_END not in existing:
        raise CompareError(
            "目标文件里找不到生成标记，无法只替换数字块；"
            f"请手工加入 {MARK_BEGIN} 与 {MARK_END}，或换一个输出路径"
        )
    head = existing.split(MARK_BEGIN, 1)[0]
    tail = existing.split(MARK_END, 1)[1]
    return head + block.rstrip("\n") + "\n\n" + tail.lstrip("\n")


def write_out(path: Path, block: str) -> str:
    """写结果，返回实际动作（新建 / 替换）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return "新建"
    updated = splice(path.read_text(encoding="utf-8"), block)
    path.write_text(updated, encoding="utf-8")
    return "替换生成块"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """命令行参数。"""
    parser = argparse.ArgumentParser(description="对比多组第十周评测结果")
    parser.add_argument("--baseline", required=True, help="基线组的报告 JSON")
    parser.add_argument(
        "--variant",
        required=True,
        nargs="+",
        help="一个或多个变体组的报告 JSON",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出文件；带标记则只刷新数字块")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    args = parse_args(argv)
    from datetime import UTC, datetime

    try:
        baseline = load_report(args.baseline)
        variants = [load_report(path) for path in args.variant]
    except CompareError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    block = render(
        baseline,
        variants,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    out = Path(args.out)
    action = write_out(out, block)

    print(f"基线   : {label_of(baseline)}（{len(baseline.get('cases', []))} 条）")
    for item in variants:
        diffs = changed_config(baseline, item)
        print(f"变体   : {label_of(item)}（改动：{'；'.join(diffs) or '无'}）")
        worse = flip_rows(baseline, item, want="worse")
        print(
            f"          变差 {len(worse)} 项，变好 {len(flip_rows(baseline, item, want='better'))} 项"
        )
    print(f"输出   : {out}（{action}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
