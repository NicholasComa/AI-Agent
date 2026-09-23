"""执行第十周评测：跑数据集、算指标、出两份报告。

用法（Git Bash，先 ``export PATH="/c/Users/Xsz/.local/bin:$PATH"``，在项目根执行）::

    # 小样本跑通（每个场景各取前 5 条）
    uv run python scripts/eval_week10_run.py --limit 5 --tag dryrun

    # 全量基线，不跑 Rubric
    uv run python scripts/eval_week10_run.py --tag baseline --judge off

    # 只跑工具调用场景（不依赖 Qdrant / Ollama，最快）
    uv run python scripts/eval_week10_run.py --scenario tool_call --tag tools

产物
----

- ``logs/eval/week10_eval_<tag>.json`` —— 机器可读，含每条用例的检查项明细与 ``trace_id``；
- ``logs/eval/week10_eval_<tag>.md`` —— 人类可读，按场景分节；
- ``logs/eval/traces/traces-<日期>.jsonl`` —— 本次评测的 trace，可直接喂给
  ``scripts/trace_week10_view.py``。

两个口径开关
------------

``--chat`` 只决定**工作流节点**的对话函数来源：

- ``fake``（缺省）注入 :func:`graph.fakes.make_fake_chat`，确定性、不调模型。
  此时 ``requirement_analysis`` 的数字反映的是**评测链路本身**是否跑通，不是
  模型质量——报告头部会原样声明这一点。
- ``real`` 复用服务启动时装配好的 ``chat_fn``，走真实模型。

``knowledge_qa`` **不受 ``--chat`` 影响**：检索（Qdrant + 嵌入模型）与生成
（``deps.chat_fn``）都是真实链路，因此该场景的数字可以直接采信，代价是每条用例
要等模型推理（本机 qwen3 约 100 秒/次，触发重召时翻倍）。Qdrant 不可用时该场景
的用例会记成执行失败并在报告里写明原因，而不是静默跳过。

为什么评测要用自己的 trace 目录
-------------------------------

执行前会把 ``OBS_BACKEND`` 固定为 ``local``、``OBS_TRACE_DIR`` 指向
``logs/eval/traces``。理由有两条：一是评测要能在没有 Langfuse 实例的机器上
跑完；二是报告里的 ``trace_id`` 必须能被 ``trace_week10_view.py`` 直接打开，
落到一个专属于评测的目录里，才不会被日常请求的记录淹没。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))  # noqa: E402

from evaluation.dataset import load_cases  # noqa: E402
from evaluation.metrics import (  # noqa: E402
    MetricResult,
    aggregate_usage,
    answer_point_coverage,
    args_schema_valid,
    category_match,
    citation_source_hit,
    clarification_expected,
    deny_enforced,
    functional_coverage,
    outcome_match,
    parse_check,
    refusal_correct,
    retrieval_hit,
    schema_valid_rate,
    tool_selected,
)
from evaluation.outcomes import (  # noqa: E402
    OUTCOME_DENIED,
    tool_outcome,
    workflow_outcome,
)
from evaluation.report import CaseResult, EvalReport, build_report  # noqa: E402
from evaluation.rubric import (  # noqa: E402
    RubricResult,
    RubricScorer,
    judge_note,
    resolve_judge_model,
    resolve_repeats,
)

DEFAULT_DATASET = PROJECT_ROOT / "data" / "golden" / "week10_golden.jsonl"
DEFAULT_OUT_DIR = PROJECT_ROOT / "logs" / "eval"
DEFAULT_TRACE_DIR = PROJECT_ROOT / "logs" / "eval" / "traces"

CHAT_MODES = ("fake", "real")
"""工作流对话函数的两种来源。"""

DEFAULT_TOP_K = 3


def _prepare_tracing(trace_dir: Path) -> None:
    """把追踪固定到本地 JSONL 后端。

    必须在 ``build_deps`` **之前**调用：``ObservabilityConfig.from_env()`` 与
    ``load_dotenv()`` 都在那一步执行，之后改环境变量不会再生效（``load_dotenv``
    默认不覆盖已存在的键，所以这里先设反而更稳）。
    """
    os.environ["OBS_BACKEND"] = "local"
    os.environ["OBS_TRACE_DIR"] = str(trace_dir)
    os.environ.setdefault("OBS_CAPTURE_CONTENT", "false")


class _CapturingRetriever:
    """转发 ``retrieve`` 并记下召回的来源顺序。

    需要它是因为 ``RagGenerator`` 内部自己调检索，调用方拿不到召回列表，而
    ``retrieval_hit@3`` 必须看真实召回。包装一层比重复检索（多付一次嵌入）
    或事后去 trace 里翻（读文件、还要按 span 配对）都更直接。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.sources: list[str] = []

    def retrieve(self, query: str, top_k: int = DEFAULT_TOP_K, **kwargs: Any) -> list[Any]:
        """检索并把命中来源按顺序记下来。"""
        hits = self._inner.retrieve(query, top_k, **kwargs)
        self.sources = [str(getattr(hit, "source", "")) for hit in hits]
        return hits


class EvalContext:
    """一次评测运行共享的依赖。"""

    def __init__(self, deps: Any, *, chat_mode: str) -> None:
        self.deps = deps
        self.chat_mode = chat_mode
        self.graph = _build_graph(deps, chat_mode)
        self._server: Any = None

    async def tool_server(self) -> Any:
        """懒构造 MCP 服务实例（只在本场景用到时才建）。"""
        if self._server is None:
            from jwipc_dev_mcp_server.config import McpServerConfig
            from jwipc_dev_mcp_server.server import build_server

            self._server = build_server(config=McpServerConfig(root=PROJECT_ROOT))
        return self._server


def _build_graph(deps: Any, chat_mode: str) -> Any:
    """按 ``chat_mode`` 组装工作流。

    ``fake`` 模式下额外把 Fake 包一层 :func:`traced_chat`，让评测的
    ``chain`` span 旁边也有 ``generation`` span——否则「用确定性替身跑」的
    trace 里只有节点名，看不出节点产生了几次模型调用。
    """
    from graph.fakes import make_fake_chat
    from graph.workflow import build_requirement_workflow
    from observability import traced_chat, traced_retriever

    if chat_mode == "fake":
        chat: Any = traced_chat(make_fake_chat(), deps.tracer, model="fake")
    else:
        # real 模式下 deps.chat_fn 已在 lifespan 里包过埋点，不能再包一层。
        chat = deps.chat_fn
    # 与 ``lifespan._build_workflow`` 同样处理：知识库自身不带埋点，工作流的检索
    # 明细只能在调用点外侧挂，否则 trace 里只有节点名、看不到召回结果。
    rag = None if deps.rag is None else traced_retriever(deps.rag, deps.tracer)
    return build_requirement_workflow(chat_fn=chat, rag=rag)


def _specs(case: Any) -> list[Any]:
    """解析用例的检查项。"""
    return [parse_check(raw) for raw in case.checks]


def _threshold(spec: Any, default: float) -> float:
    """取检查项里的阈值；没写就用缺省。"""
    return default if spec.threshold is None else spec.threshold


# ----------------------------------------------------------------------
# 三个场景的执行器
# ----------------------------------------------------------------------


async def run_knowledge_qa(case: Any, ctx: EvalContext, tracer: Any) -> tuple[CaseResult, str]:
    """走真实检索 + 真实生成。"""
    from observability import traced_retriever
    from rag.generator import RagGenerator

    deps = ctx.deps
    capture = _CapturingRetriever(deps.rag)
    # 生成器调的是 ``retrieve()``，而知识库内部走 ``self._retriever.search()``，
    # 会绕过挂在检索器上的那层埋点；不在这里补一层，用例的 trace 里就没有
    # retriever span，``retrieval_hit`` 一失败便无从判断是「没召回」还是
    # 「召回了没答对」——而后者恰恰是本周要能区分的东西。
    traced = traced_retriever(capture, tracer, min_score=case.input.min_score)
    generator = RagGenerator(
        traced,
        deps.chat_fn,
        top_k=case.input.top_k,
        min_score=case.input.min_score,
    )
    answer = await generator.answer(case.input.query)

    citations = [str(item.source) for item in answer.citations]
    metrics: list[MetricResult] = []
    for spec in _specs(case):
        if spec.name == "retrieval_hit":
            metrics.append(
                retrieval_hit(
                    capture.sources,
                    case.reference.expect_sources,
                    k=spec.k or DEFAULT_TOP_K,
                )
            )
        elif spec.name == "citation_source_hit":
            metrics.append(citation_source_hit(citations, case.reference.expect_sources))
        elif spec.name == "answer_point_coverage":
            metrics.append(
                answer_point_coverage(
                    answer.answer,
                    case.reference.expect_points,
                    threshold=_threshold(spec, 0.6),
                )
            )
        elif spec.name == "refusal_correct":
            metrics.append(refusal_correct(bool(answer.has_answer), case.reference.expect_found))

    return _finish(case, metrics), answer.answer


async def run_requirement_analysis(
    case: Any, ctx: EvalContext, tracer: Any
) -> tuple[CaseResult, str]:
    """走完整工作流，节点埋点经回调处理器落到同一条 trace 上。"""
    from observability import ObservabilityCallbackHandler

    handler = ObservabilityCallbackHandler(tracer)
    config = {
        "configurable": {"thread_id": f"eval-{case.id}"},
        "callbacks": [handler],
    }
    state = await ctx.graph.ainvoke({"requirement_text": case.input.text}, config=config)
    outcome, reason = workflow_outcome(state)

    if outcome is None:
        # 产出不合规时，其余检查项无从判定，记 skip 而不是失败——失败原因已经
        # 由 schema_valid_rate 一条说清，把其它项也标红只会淹没真正的信号。
        metrics = [schema_valid_rate(False)]
        for spec in _specs(case):
            if spec.name != "schema_valid_rate":
                metrics.append(MetricResult.skipped_result(spec.name, f"产出不合规：{reason}"))
        result = _finish(case, metrics)
        result.error = reason
        return result, ""

    metrics = []
    for spec in _specs(case):
        if spec.name == "category_match":
            metrics.append(category_match(outcome.category, case.reference.expect_category))
        elif spec.name == "clarification_expected":
            metrics.append(
                clarification_expected(outcome.needs_clarify, case.reference.expect_needs_clarify)
            )
        elif spec.name == "functional_coverage":
            metrics.append(
                functional_coverage(
                    outcome.functional_points,
                    case.reference.expect_points,
                    threshold=_threshold(spec, 0.6),
                )
            )
        elif spec.name == "schema_valid_rate":
            metrics.append(schema_valid_rate(True))

    produced = "\n".join(outcome.functional_points) or "；".join(outcome.clarification_questions)
    return _finish(case, metrics), produced


async def run_tool_call(case: Any, ctx: EvalContext, tracer: Any) -> tuple[CaseResult, str]:
    """直接调 MCP 工具，不经传输层。"""
    server = await ctx.tool_server()
    schema_ok = True
    schema_detail = "参数通过工具 schema 校验"
    structured: dict[str, Any] | None = None
    try:
        _, structured = await server.call_tool(case.input.tool, case.input.arguments)
    except Exception as exc:  # noqa: BLE001 —— 参数不合规会在这里抛出
        schema_ok = False
        schema_detail = f"{type(exc).__name__}: {exc}"

    outcome, kind = tool_outcome(structured)
    expect_denied = case.reference.expect_outcome == OUTCOME_DENIED
    metrics: list[MetricResult] = []
    for spec in _specs(case):
        if spec.name == "tool_selected":
            metrics.append(tool_selected(case.input.tool, case.reference.expect_tool))
        elif spec.name == "args_schema_valid":
            metrics.append(args_schema_valid(schema_ok, schema_detail))
        elif spec.name == "outcome_match":
            metrics.append(outcome_match(outcome, case.reference.expect_outcome, kind=kind))
        elif spec.name == "deny_enforced":
            metrics.append(deny_enforced(outcome, expect_denied=expect_denied, kind=kind))
    # 工具场景不产出自然语言，不参与 Rubric。
    return _finish(case, metrics), ""


def _finish(case: Any, metrics: list[MetricResult]) -> CaseResult:
    """把检查项收成一条用例结果。"""
    failed = [item for item in metrics if not item.passed and not item.skipped]
    return CaseResult(
        id=case.id,
        scenario=case.scenario,
        group=case.group,
        passed=not failed,
        metrics=metrics,
    )


# ----------------------------------------------------------------------
# 编排
# ----------------------------------------------------------------------


async def _run_case(case: Any, ctx: EvalContext, tracer: Any) -> tuple[CaseResult, str]:
    """在一条 trace 里跑完单条用例。

    Returns:
        ``(结果, 产出文本)``。产出文本交给 Rubric 打分，只在进程内传递、不落盘。
    """
    runner = {
        "knowledge_qa": run_knowledge_qa,
        "requirement_analysis": run_requirement_analysis,
        "tool_call": run_tool_call,
    }[case.scenario]

    started = time.perf_counter()
    with tracer.trace(f"eval.{case.scenario}", request_id=case.id, group=case.group) as root:
        trace_id = root.trace_id
        try:
            result, produced = await runner(case, ctx, tracer)
        except Exception as exc:  # noqa: BLE001 —— 单条用例失败不该中断整轮评测
            result = CaseResult(
                id=case.id,
                scenario=case.scenario,
                group=case.group,
                passed=False,
                metrics=[],
                error=f"{type(exc).__name__}: {exc}",
            )
            produced = ""
    result.trace_id = trace_id
    result.latency_ms = (time.perf_counter() - started) * 1000.0
    return result, produced


async def _judge_all(
    cases: Sequence[Any],
    results: Sequence[CaseResult],
    produced: dict[str, str],
    ctx: EvalContext,
) -> list[RubricResult]:
    """对所有产出了文本的用例跑 Rubric。"""
    model = resolve_judge_model(os.getenv("MODEL_NAME"))
    scorer = RubricScorer(
        ctx.deps.chat_fn,
        model=model,
        repeats=resolve_repeats(),
        tracer=ctx.deps.tracer,
    )
    by_id = {case.id: case for case in cases}
    out: list[RubricResult] = []
    for result in results:
        text = produced.get(result.id, "")
        if not text:
            continue
        case = by_id[result.id]
        question = case.input.query if case.scenario == "knowledge_qa" else case.input.text
        out.append(await scorer.score(answer_id=result.id, question=question, answer=text))
    return out


def _usage_from_traces(trace_dir: Path, trace_ids: set[str]) -> dict[str, float]:
    """从本次评测的 JSONL 里汇总 token 与成本。

    报告要能回答「这一轮花了多少」，而 usage 只写在 span 上，因此必须回读。
    只统计本次记录下的 ``trace_id``，避免把同目录下别的运行也算进来。
    """
    usages: list[dict[str, Any]] = []
    for path in sorted(trace_dir.glob("traces-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if str(row.get("trace_id") or "") not in trace_ids:
                continue
            usage = row.get("usage")
            if isinstance(usage, dict):
                usages.append(usage)
    return aggregate_usage(usages)


def _display_path(path: Path) -> str:
    """给报告用的路径：能相对项目根就相对，且一律用正斜杠。

    报告里的路径会直接落进 Markdown 的排查命令里，Windows 反斜杠在 Git Bash 下
    会被当转义符吃掉（``logs\\eval\\traces`` 变成 ``logsevaltraces``），照抄即错。
    路径不在项目根下时退回绝对路径，不让 ``relative_to`` 抛异常把整轮评测打断。
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _select(cases: list[Any], *, scenario: str | None, limit: int | None) -> list[Any]:
    """按场景过滤并逐场景取前 N 条。

    ``--limit`` 是**每个场景**的条数上限，不是总数上限：否则报告里只会剩下
    排在最前的那个场景，而「按场景统计」也就无从谈起。
    """
    picked = cases if scenario is None else [c for c in cases if c.scenario == scenario]
    if limit is None:
        return picked
    grouped: dict[str, list[Any]] = {}
    for case in picked:
        grouped.setdefault(case.scenario, []).append(case)
    out: list[Any] = []
    for scenario_name in sorted(grouped):
        out.extend(grouped[scenario_name][:limit])
    return out


async def _main_async(args: argparse.Namespace) -> int:
    """异步主流程。"""
    trace_dir = Path(args.trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    _prepare_tracing(trace_dir)

    from agent_service.lifespan import build_deps

    deps = await build_deps()
    ctx = EvalContext(deps, chat_mode=args.chat)

    dataset = Path(args.dataset)
    all_cases = load_cases(dataset)
    cases = _select(all_cases, scenario=args.scenario, limit=args.limit)
    if not cases:
        print(f"[FAIL] 没有选中任何用例（dataset={dataset} scenario={args.scenario}）")
        return 1

    print(
        f"数据集 {dataset.name}：共 {len(all_cases)} 条，本次执行 {len(cases)} 条"
        f"（scenario={args.scenario or 'all'} limit={args.limit} chat={args.chat}）"
    )

    results: list[CaseResult] = []
    produced: dict[str, str] = {}
    for case in cases:
        result, text = await _run_case(case, ctx, deps.tracer)
        if text:
            produced[result.id] = text
        results.append(result)
        mark = "pass" if result.passed else "FAIL"
        print(f"  [{mark}] {result.id:9} {result.scenario:20} {result.group}")

    notes: list[str] = []
    if args.chat == "fake":
        notes.append(
            "chat=fake：requirement_analysis 的全部节点使用确定性替身，"
            "故 category_match / clarification_expected / functional_coverage / schema_valid_rate "
            "只反映评测链路是否跑通，不代表模型质量"
        )
        notes.append(
            "knowledge_qa 不受 chat 模式影响：检索（Qdrant + 嵌入模型）与生成（真实模型）都是真实链路，"
            "其数字可直接采信"
        )
    if deps.rag is None:
        notes.append("Qdrant 未就绪，knowledge_qa 用例会记为执行失败")

    rubric: list[RubricResult] = []
    if args.judge == "on":
        rubric = await _judge_all(cases, results, produced, ctx)
        notes.append(judge_note(rubric[0].model if rubric else None))

    usage = _usage_from_traces(trace_dir, {r.trace_id for r in results if r.trace_id})
    report: EvalReport = build_report(
        results,
        tag=args.tag,
        dataset=_display_path(dataset),
        dataset_size=len(all_cases),
        chat_mode=args.chat,
        retrieval_mode="real (qdrant + embedding)",
        trace_dir=_display_path(trace_dir),
        scenario=args.scenario,
        notes=notes,
        usage=usage,
    )
    report.rubric = rubric

    out_dir = Path(args.out_dir)
    json_path = report.to_json(out_dir / f"week10_eval_{args.tag}.json")
    md_path = out_dir / f"week10_eval_{args.tag}.md"
    md_path.write_text(report.to_markdown(), encoding="utf-8")

    total = len(results)
    passed = sum(1 for item in results if item.passed)
    print(f"\n通过 {passed}/{total}")
    print(f"JSON     : {json_path}")
    print(f"Markdown : {md_path}")
    print(f"trace    : {trace_dir}")
    print(
        "查看失败用例：uv run python scripts/trace_week10_view.py "
        f"--trace-id <trace_id> --dir {_display_path(trace_dir)}"
    )
    return 0 if passed == total else 1


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="执行第十周 Golden Dataset 评测")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="数据集路径")
    parser.add_argument("--scenario", choices=("knowledge_qa", "requirement_analysis", "tool_call"))
    parser.add_argument("--limit", type=int, default=None, help="每个场景取前 N 条")
    parser.add_argument("--tag", default="baseline", help="结果标签，决定输出文件名")
    parser.add_argument("--judge", choices=("on", "off"), default="off", help="是否跑 Rubric")
    parser.add_argument("--chat", choices=CHAT_MODES, default="fake", help="工作流对话函数来源")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="报告输出目录")
    parser.add_argument("--trace-dir", default=str(DEFAULT_TRACE_DIR), help="本次评测的 trace 目录")
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
