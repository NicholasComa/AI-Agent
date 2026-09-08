"""命令行入口：演示 RequirementAnalysisWorkflow 的两条主路径。

用法（Git Bash，先 export PATH="/c/Users/Xsz/.local/bin:$PATH"）：

    uv run python scripts/graph_week8_workflow.py
    uv run python scripts/graph_week8_workflow.py --requirement "我想做个面向学校的考勤系统"
    uv run python scripts/graph_week8_workflow.py --clarify "我想做个东西" --resume "面向零售商的智能推荐系统"

参数规则：
  - 只传 --requirement        → 只演示正常 6 节点路径
  - 只传 --clarify [--resume] → 只演示歧义澄清 + resume 路径
  - 两个都不传               → 两条路径都跑（默认演示）
  - 两个都传                 → 两条路径都跑

注意：下游的 functional_points / risks / test_points 由注入的 Fake ChatFn
返回固定样例（用于确定性测试），与具体需求文本无关；周四接入真实模型后
会改为需求定制内容。classify（分类与置信度）则真实读取输入文本。

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

DEFAULT_REQUIREMENT = "我们公司要做面向零售商的 B2C 电商网站，支持登录、商品、购物车、支付、后台。"
DEFAULT_CLARIFY = "我想做个东西"
DEFAULT_RESUME = "面向零售商的智能推荐系统"


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


async def _run_normal(requirement: str, thread_id: str) -> None:
    """正常需求：完整 6 节点跑通，打印 trace 与关键字段。"""
    _section(f"正常路径：6 节点依次执行（需求：{requirement}）")
    wf = build_requirement_workflow()
    res = await wf.ainvoke(
        {"requirement_text": requirement},
        {"configurable": {"thread_id": f"{thread_id}-normal"}},
    )
    # classify 可能把输入判为需澄清并路由到 clarify 中断，此时不会走完 6 节点
    if res.get("needs_clarify"):
        print("该需求被 classify 判定为需澄清，已路由到 clarify 节点（interrupt），未走完 6 节点。")
        print("澄清问题 =", res.get("clarification_questions"))
        print("提示：用 --clarify 单独演示澄清分支，或换信息更完整的 --requirement。")
        return
    print("trace         =", res["trace"])
    print("category/conf =", res["category"], res["confidence"])
    print("functional_points =", res["functional_points"])
    print("risks         =", res["risks"])
    print("test_points   =", res["test_points"])
    print("rag_degraded  =", res["rag_degraded"])
    print("report 前 2 行 =", res["report"].splitlines()[:2])
    print(
        "注：功能点/风险/测试点为 Fake 固定样例（确定性测试用），与具体需求文本无关；"
        "周四接入真实模型后将为需求定制内容。"
    )


async def _run_ambiguous(clarify: str, resume: str, thread_id: str) -> None:
    """歧义需求：触发 clarify 中断，人工补充后 resume 续跑。"""
    _section(f"歧义路径：触发 clarify 中断 + 人工补充后续跑（需求：{clarify} / 补充：{resume}）")
    wf = build_requirement_workflow()
    cfg = {"configurable": {"thread_id": f"{thread_id}-ambiguous"}}
    async for event in wf.astream({"requirement_text": clarify}, cfg, stream_mode="updates"):
        print("event =", event)
    state = wf.get_state(cfg)
    print("挂起 next =", state.next)
    print("澄清问题 =", state.values.get("clarification_questions"))
    resumed = await wf.ainvoke(Command(resume=resume), cfg)
    print("resume 后 human_answers =", resumed["human_answers"])
    print("resume 后 trace         =", resumed["trace"])


async def main_async(args: argparse.Namespace) -> None:
    has_req = args.requirement is not None
    has_clar = args.clarify is not None
    # 只传其一 → 只跑对应路径；都不传或都传 → 两条都跑
    run_normal = not has_clar
    run_ambiguous = not has_req
    if not has_req and not has_clar:
        run_normal = run_ambiguous = True
    if run_normal:
        await _run_normal(args.requirement or DEFAULT_REQUIREMENT, args.thread_id)
    if run_ambiguous:
        await _run_ambiguous(args.clarify or DEFAULT_CLARIFY, args.resume, args.thread_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行 RequirementAnalysisWorkflow 演示。")
    parser.add_argument(
        "--requirement",
        default=None,
        help="正常需求文本；只传此参数时只演示正常 6 节点路径。",
    )
    parser.add_argument(
        "--clarify",
        default=None,
        help="歧义需求文本；只传此参数时只演示 clarify 中断与 resume。",
    )
    parser.add_argument(
        "--resume",
        default=DEFAULT_RESUME,
        help="模拟人工补充的内容，作为 interrupt() 的返回值回填（配合 --clarify 使用）。",
    )
    parser.add_argument("--thread-id", default="wf-demo", help="Checkpoint 线程前缀。")
    args = parser.parse_args(argv)
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
