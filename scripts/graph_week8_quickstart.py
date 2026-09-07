"""命令行入口：演示最小状态图的条件分支、人工中断与 Checkpoint 续跑。

用法（Git Bash，先 export PATH="/c/Users/Xsz/.local/bin:$PATH"）：

    uv run python scripts/graph_week8_quickstart.py
    uv run python scripts/graph_week8_quickstart.py --question "报表要支持导出?"
    uv run python scripts/graph_week8_quickstart.py --question "接口要做分页?" --resume "每页 20 条"

``--resume`` 不传时会按 ``--question`` 自动生成补充内容，因此演示输出始终自洽。

脚本需要先把 ``src/`` 加入模块搜索路径，因此本地导入位于 ``sys.path``
操作之后；pyproject 已为本脚本豁免 E402。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langgraph.types import Command  # noqa: E402

from graph.quickstart import build_quickstart_graph  # noqa: E402


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def main(argv: list[str] | None = None) -> int:
    """按 事件序列 → 挂起状态 → 续跑 → 直跑 → 架构图 的顺序演示一遍。"""
    parser = argparse.ArgumentParser(description="运行最小 LangGraph 状态图示例。")
    parser.add_argument(
        "--question",
        default="这个报表要支持导出?",
        help="输入问题；以问号结尾会触发人工澄清分支。",
    )
    parser.add_argument(
        "--resume",
        default=None,
        help=(
            "模拟人工补充的内容，会作为 interrupt() 的返回值回填。"
            "不传时按 --question 自动生成，保证演示输出自洽。"
        ),
    )
    parser.add_argument("--thread-id", default="quickstart-1", help="Checkpoint 线程标识。")
    args = parser.parse_args(argv)

    app = build_quickstart_graph()
    config = {"configurable": {"thread_id": args.thread_id}}

    _section("1. 事件序列（stream_mode=updates）")
    for event in app.stream({"question": args.question}, config, stream_mode="updates"):
        print(event)

    state = app.get_state(config)
    _section("2. 挂起状态")
    print("next   =", state.next)
    print("values =", state.values)

    if state.next:
        _section("3. 人工补充后继续（Command(resume=...)）")
        resume_text = args.resume or f"按「{args.question.rstrip('?？')}」原意执行"
        print("resume  =", resume_text)
        resumed = app.invoke(Command(resume=resume_text), config)
        print("最终状态 =", resumed)

    _section("4. 无歧义输入直跑（独立线程）")
    direct_config = {"configurable": {"thread_id": f"{args.thread_id}-direct"}}
    direct_question = args.question.rstrip("?？") + "。"
    print(app.invoke({"question": direct_question}, direct_config))

    _section("5. Mermaid 架构图")
    print(app.get_graph().draw_mermaid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
