"""命令行入口：需求分析工作流的业务串联演示（1 正常 + 3 异常）。

用法（Git Bash，先 export PATH="/c/Users/Xsz/.local/bin:$PATH"）：

    uv run python scripts/graph_week8_demo.py
    uv run python scripts/graph_week8_demo.py --mermaid
    uv run python scripts/graph_week8_demo.py --scenario normal --requirement "我要做一个考勤系统"
    uv run python scripts/graph_week8_demo.py --scenario rag_down
    uv run python scripts/graph_week8_demo.py --scenario normal --requirement data/raw/需求.txt --real
    uv run python scripts/graph_week8_demo.py --scenario normal --real --resume "仅 Web 版，不做移动端"

四个场景：

- ``normal``：完整需求走满 6 节点，报告含功能点 / 风险 / 测试点 / 引用来源。
  真实模型若判定需求存在歧义，工作流会在 ``clarify`` 处中断挂起；此时脚本打印
  澄清问题，并提示补 ``--resume`` 续跑（携带该参数时自动从断点续跑到完整报告）。
- ``ambiguity``：需求缺关键信息，在 classify 后中断并列出澄清问题，
  用 ``--resume`` 补充后从中断点继续，人工答案参与后续生成。
- ``rag_down``：依赖降级。注入一个不可用的检索器（等价于 Qdrant 未启动），
  ``rag_degraded`` 置 True，工作流不中断，报告标注未检索到内部资料。
- ``llm_fail``：节点失败。注入持续报错的 chat_fn，重试耗尽后写 ``errors``
  并降级，报告仍产出并说明降级。

``--resume`` 对 ``normal`` 与 ``ambiguity`` 两个场景生效：normal 场景仅在真实
模型判歧义触发中断后使用；ambiguity 场景缺省按 ``--ambiguous`` 自动生成人工答复。

``--real`` 按 ``.env`` 接真实 Ollama 对话模型与 Qdrant 知识库；构造失败
（服务未起 / 缺少配置）时自动回退到 Fake 并在输出首行打印回退原因，避免
演示卡死。``rag_down`` 与 ``llm_fail`` 两个异常场景依赖注入的故障件，始终走 Fake。

``--requirement`` 既接受需求文本，也接受文本文件路径（文件存在时按其
内容读取）。

脚本需要先把 src/ 加入模块搜索路径，因此本地导入位于 sys.path
操作之后；pyproject 已为本脚本豁免 E402。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langgraph.types import Command  # noqa: E402

from graph.workflow import build_requirement_workflow  # noqa: E402

DEFAULT_REQUIREMENT = (
    "我们公司要做一个面向中小型零售商的 B2C 电商网站，"
    "支持登录、商品浏览与搜索、购物车、订单管理、支付、后台管理。"
)
DEFAULT_AMBIGUOUS = "我想做个东西"
DEFAULT_RESUME = "面向零售商的智能推荐系统"
SCENARIOS = ("normal", "ambiguity", "rag_down", "llm_fail", "all")

MERMAID = """
---
config:
  flowchart:
    curve: linear
---
graph TD;
    __start__([__start__]):::startEnd
    classify(classify):::decision
    clarify(clarify):::human
    functional_points(functional_points):::work
    rag_retrieve(rag_retrieve):::work
    risk(risk):::work
    test_points(test_points):::work
    report(report):::work
    __end__([__end__]):::startEnd
    __start__ --> classify;
    classify -.-> clarify;
    classify -.-> functional_points;
    clarify --> functional_points;
    functional_points --> rag_retrieve;
    rag_retrieve --> risk;
    risk --> test_points;
    test_points --> report;
    report --> __end__;

    classDef startEnd fill:#1565c0,stroke:#0d47a1,stroke-width:3px,color:#ffffff
    classDef decision fill:#ff9800,stroke:#e65100,stroke-width:3px,color:#212121
    classDef human fill:#2e7d32,stroke:#1b5e20,stroke-width:3px,color:#ffffff
    classDef work fill:#e3f2fd,stroke:#1565c0,stroke-width:2px,color:#0d47a1
    classDef fallback fill:#ffebee,stroke:#c62828,stroke-width:2px,color:#b71c1c
""".strip()


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _read_requirement(value: str) -> str:
    """把 --requirement 解析为文本：文件路径优先按文件读取。"""
    path = Path(value)
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return value


class _UnavailableRAG:
    """模拟知识库不可用（等价于 Qdrant 未启动）：检索一律抛连接错误。"""

    def retrieve(self, query: str, top_k: int = 3):  # noqa: ARG002, D102
        raise ConnectionError("Qdrant 未启动，连接被拒绝")


def _failing_chat():
    """构造一个持续报错的 chat_fn，用于演示节点失败后的降级。"""

    async def _chat(_messages):
        raise RuntimeError("LLM 服务不可用（模拟持续故障）")

    return _chat


def _build_real_deps():
    """按 .env 构造真实依赖：返回 (chat_fn, rag, closer)。

    chat_fn 适配 LlmClient.chat 的 LlmResult（取 text 字段），并去掉节点
    内部消息里用于路由的 task 键，只保留 role / content 两个标准字段。
    """
    from dotenv import load_dotenv

    load_dotenv()

    from config import load_config  # noqa: PLC0415
    from llm_client import LlmClient  # noqa: PLC0415
    from rag.embeddings import get_embedding  # noqa: PLC0415
    from rag.knowledge_rag import (  # noqa: PLC0415
        JwipcKnowledgeRAG,
        build_qdrant_config,
    )

    app_cfg = load_config()
    client = LlmClient(
        base_url=app_cfg.api_base_url,
        model=app_cfg.model_name,
        timeout_seconds=app_cfg.timeout_seconds,
    )

    async def _chat(messages):
        plain = [{"role": m["role"], "content": m["content"]} for m in messages]
        result = await client.chat(plain)
        return result.text

    embedder = get_embedding()
    collection = os.environ.get("QDRANT_COLLECTION", "jwipc_v3")
    rag = JwipcKnowledgeRAG(embedder, build_qdrant_config(collection, embedder))
    return _chat, rag, client.aclose


def _print_result(state: dict) -> None:
    """打印一次执行的关键状态字段与完整报告。"""
    print("trace        =", state.get("trace"))
    print("category/conf=", state.get("category"), state.get("confidence"))
    print("functional   =", state.get("functional_points"))
    print("risks        =", state.get("risks"))
    print("test_points  =", state.get("test_points"))
    print("rag_degraded =", state.get("rag_degraded"))
    print("errors       =", state.get("errors", []))
    report = state.get("report") or ""
    print("report:")
    print(report if report.strip() else "  (空)")


async def _scenario_normal(
    requirement: str, thread_id: str, real: bool, resume: str | None
) -> None:
    """正常：完整需求走满 6 节点，产出完整报告。

    真实模型若判定需求存在歧义，工作流会在 clarify 处中断（interrupt）。
    此时不再输出空结果：打印澄清问题并提示；若同时携带 --resume，则从中断
    点续跑到完整报告，验证端到端链路。
    """
    _section(f"场景 1 正常流程（real={real}）")
    chat_fn = rag = None
    closer = None
    if real:
        try:
            chat_fn, rag, closer = _build_real_deps()
        except Exception as exc:  # 真实依赖不可用 -> 回退 Fake 并说明原因
            print(f"真实链路不可用，已回退 Fake：{type(exc).__name__}: {exc}")
            chat_fn = rag = None
    wf = build_requirement_workflow(chat_fn=chat_fn, rag=rag)
    cfg = {"configurable": {"thread_id": f"{thread_id}-normal"}}
    try:
        state = await wf.ainvoke({"requirement_text": requirement}, cfg)
        # 检测歧义中断：真实模型可能把需求判为需要澄清
        pending = wf.get_state(cfg)
        if pending.next and "clarify" in pending.next:
            questions = pending.values.get("clarification_questions") or []
            print("需求被判为需要澄清，工作流已在 clarify 处中断。")
            print("澄清问题   =", questions)
            if resume:
                print(f"使用 --resume 续跑，人工补充 = {resume!r}")
                resumed = await wf.ainvoke(Command(resume=resume), cfg)
                _print_result(resumed)
                return
            print("提示：加 --resume '<人工补充>' 可从中断点续跑并产出完整报告。")
            return
        _print_result(state)
    finally:
        if closer is not None:
            await closer()


async def _scenario_ambiguity(requirement: str, resume: str, thread_id: str) -> None:
    """异常 1：歧义暂停 —— 中断列出澄清问题，人工补充后续跑。"""
    _section("场景 2 歧义暂停（人工确认）")
    wf = build_requirement_workflow()
    cfg = {"configurable": {"thread_id": f"{thread_id}-ambiguity"}}
    async for event in wf.astream({"requirement_text": requirement}, cfg, stream_mode="updates"):
        print("event =", event)
    pending = wf.get_state(cfg)
    print("挂起 next  =", pending.next)
    print("澄清问题   =", pending.values.get("clarification_questions"))
    resumed = await wf.ainvoke(Command(resume=resume), cfg)
    print("人工补充   =", resumed.get("human_answers"))
    _print_result(resumed)


async def _scenario_rag_down(requirement: str, thread_id: str) -> None:
    """异常 2：依赖降级 —— 知识库不可用，图继续并标注依据不足。"""
    _section("场景 3 依赖降级（知识库不可用）")
    wf = build_requirement_workflow(rag=_UnavailableRAG())
    state = await wf.ainvoke(
        {"requirement_text": requirement},
        {"configurable": {"thread_id": f"{thread_id}-ragdown"}},
    )
    assert state["rag_degraded"] is True, "知识库不可用时应置 rag_degraded"
    _print_result(state)
    print("报告含降级标注 =", "未检索到内部资料" in state.get("report", ""))


async def _scenario_llm_fail(requirement: str, thread_id: str) -> None:
    """异常 3：节点失败 —— 重试耗尽写 errors 并降级，报告仍产出。"""
    _section("场景 4 节点失败（重试后降级）")
    wf = build_requirement_workflow(chat_fn=_failing_chat())
    state = await wf.ainvoke(
        {"requirement_text": requirement},
        {"configurable": {"thread_id": f"{thread_id}-llmfail"}},
    )
    _print_result(state)
    failed = [entry["node"] for entry in state.get("errors", [])]
    print("失败节点     =", failed)


async def main_async(args: argparse.Namespace) -> None:
    if args.mermaid:
        print(MERMAID)
        return
    requirement = _read_requirement(args.requirement)
    chosen = args.scenario
    if chosen in ("normal", "all"):
        await _scenario_normal(requirement, args.thread_id, args.real, args.resume)
    if chosen in ("ambiguity", "all"):
        await _scenario_ambiguity(args.ambiguous, args.resume or DEFAULT_RESUME, args.thread_id)
    if chosen in ("rag_down", "all"):
        await _scenario_rag_down(requirement, args.thread_id)
    if chosen in ("llm_fail", "all"):
        await _scenario_llm_fail(requirement, args.thread_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="需求分析工作流业务串联演示。")
    parser.add_argument(
        "--scenario",
        default="all",
        choices=list(SCENARIOS),
        help="要演示的场景；all 表示四个场景依次跑。",
    )
    parser.add_argument(
        "--requirement",
        default=DEFAULT_REQUIREMENT,
        help="需求文本或文本文件路径（文件存在时按内容读取）。",
    )
    parser.add_argument(
        "--ambiguous",
        default=DEFAULT_AMBIGUOUS,
        help="歧义场景的输入文本（信息不足才会触发中断）。",
    )
    parser.add_argument(
        "--resume",
        default=None,
        help="歧义场景模拟人工补充的内容；normal 场景携带本参数时，若真实模型判定需求歧义则在中断点续跑。",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="按 .env 接真实 Ollama 与 Qdrant；不可用时自动回退 Fake，并在输出首行打印回退原因。",
    )
    parser.add_argument(
        "--mermaid",
        action="store_true",
        help="打印带高对比度样式 classDef 的 Mermaid 图源码，不执行业务场景。",
    )
    parser.add_argument("--thread-id", default="demo", help="Checkpoint 线程前缀。")
    args = parser.parse_args(argv)
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
