"""命令行入口：演示 RequirementAnalysisWorkflow 的两条主路径。

用法（Git Bash，先 export PATH="/c/Users/Xsz/.local/bin:$PATH"）：

    uv run python scripts/graph_week8_workflow.py
    uv run python scripts/graph_week8_workflow.py --requirement "我想做个面向学校的考勤系统"
    uv run python scripts/graph_week8_workflow.py --clarify "我想做个东西" --resume "面向零售商的智能推荐系统"

默认会跑两条路径：
  1. 正常需求：6 节点依次写满状态，打印 trace 与关键字段。
  2. 歧义需求：classify 判定需澄清 → 触发 interrupt 挂起 → 人工补充后 resume 续跑。

脚本需要先把 src/ 加入模块搜索路径，因此本地导入位于 sys.path
操作之后；pyproject 已为本脚本豁免 E402。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langgraph.types import Command  # noqa: E402

from graph.workflow import build_requirement_workflow  # noqa: E402


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


async def _run_normal(requirement: str, thread_id: str) -> None:
    """正常需求：完整 6 节点跑通，打印 trace 与关键字段。"""
    _section("正常路径：6 节点依次执行")
    wf = build_requirement_workflow()
    res = await wf.ainvoke(
        {"requirement_text": requirement},
        {"configurable": {"thread_id": f"{thread_id}-normal"}},
    )
    print("trace         =", res["trace"])
    print("category/conf =", res["category"], res["confidence"])
    print("functional_points =", res["functional_points"])
    print("risks         =", res["risks"])
    print("test_points   =", res["test_points"])
    print("rag_degraded  =", res["rag_degraded"])
    print("report 前 2 行 =", res["report"].splitlines()[:2])


async def _run_ambiguous(clarify: str, resume: str, thread_id: str) -> None:
    """歧义需求：触发 clarify 中断，人工补充后 resume 续跑。"""
    _section("歧义路径：触发 clarify 中断 + 人工补充后续跑")
    wf = build_requirement_workflow()
    cfg = {"configurable": {"thread_id": f"{thread_id}-ambiguous"}}
    print("输入 =", clarify)
    async for event in wf.astream({"requirement_text": clarify}, cfg, stream_mode="updates"):
        print("event =", event)
    state = wf.get_state(cfg)
    print("挂起 next =", state.next)
    print("澄清问题 =", state.values.get("clarification_questions"))
    resumed = await wf.ainvoke(Command(resume=resume), cfg)
    print("resume 后 human_answers =", resumed["human_answers"])
    print("resume 后 trace         =", resumed["trace"])


async def main_async(args: argparse.Namespace) -> None:
    await _run_normal(args.requirement, args.thread_id)
    await _run_ambiguous(args.clarify, args.resume, args.thread_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行 RequirementAnalysisWorkflow 演示。")
    parser.add_argument(
        "--requirement",
        default="我们公司要做面向零售商的 B2C 电商网站，支持登录、商品、购物车、支付、后台。",
        help="正常需求文本，用于演示 6 节点完整执行。",
    )
    parser.add_argument(
        "--clarify",
        default="我想做个东西",
        help="歧义需求文本，用于演示 clarify 中断与 resume。",
    )
    parser.add_argument(
        "--resume",
        default="面向零售商的智能推荐系统",
        help="模拟人工补充的内容，作为 interrupt() 的返回值回填。",
    )
    parser.add_argument("--thread-id", default="wf-demo", help="Checkpoint 线程前缀。")
    args = parser.parse_args(argv)
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
