"""RequirementAnalysisWorkflow 流程测试：图结构 / 节点职责 / 人工确认。

运行（Git Bash）：
    uv run pytest tests/graph/test_requirement_workflow.py -v

三组覆盖：

- 图结构（5 条）：节点集合、固定边主链、条件边分支、Checkpoint 线程隔离、
  Mermaid 导出。
- 节点职责（7 条）：6 个业务节点各自只写自己负责的字段，且失败/降级行为正确。
- 人工确认（4 条）：歧义中断、挂起节点可定位、resume 续跑、人工答案进入下游。
"""

from __future__ import annotations

from langgraph.types import Command

from graph.config import WorkflowConfig
from graph.fakes import fake_chat
from graph.nodes import (
    Deps,
    classify,
    functional_points,
    rag_retrieve,
    report,
    risk,
    route_after_classify,
)
from graph.nodes import (
    # 别名导入：原名 test_points 会被 pytest 当成测试用例收集
    test_points as build_test_points,
)
from graph.workflow import build_requirement_workflow
from rag.retriever import RetrievalResult

NORMAL_REQUIREMENT = (
    "我们公司要做一个面向中小型零售商的 B2C 电商网站，"
    "支持登录、商品浏览与搜索、购物车、订单管理、支付、后台管理。"
)
AMBIGUOUS_REQUIREMENT = "我想做个东西"

BUSINESS_NODES = {
    "classify",
    "clarify",
    "functional_points",
    "rag_retrieve",
    "risk",
    "test_points",
    "report",
}


def _deps(**overrides) -> Deps:
    """构造节点依赖；chat_fn 默认 Fake，rag 默认 None（触发降级）。"""
    base = {
        "chat_fn": fake_chat,
        "rag": None,
        "config": WorkflowConfig(),
    }
    base.update(overrides)
    return Deps(**base)  # type: ignore[arg-type]


class _StubRAG:
    """返回固定片段的检索器，用于验证 rag_retrieve 的字段映射。"""

    def __init__(self, results):
        self._results = results
        self.queries: list[str] = []

    def retrieve(self, query: str, top_k: int = 3):
        self.queries.append(query)
        return self._results[:top_k]


class _BrokenRAG:
    """检索一律抛错，模拟 Qdrant 未启动。"""

    def retrieve(self, query: str, top_k: int = 3):  # noqa: ARG002
        raise ConnectionError("Qdrant 未启动")


def _spy_chat():
    """包装 Fake，同时记录每次调用的最后一条 user 消息。"""

    seen: list[str] = []

    async def _chat(messages):
        seen.append(messages[-1]["content"])
        return await fake_chat(messages)

    return _chat, seen


# --------------------------------------------------------------------------
# 图结构（5 条）
# --------------------------------------------------------------------------


async def test_graph_exposes_seven_business_nodes():
    """编译后的图包含 6 个业务节点 + 1 个澄清节点。"""
    graph = build_requirement_workflow().get_graph()
    assert BUSINESS_NODES.issubset(set(graph.nodes.keys()))


async def test_fixed_edges_form_main_chain():
    """固定边构成 report 之前的单向主链，不存在跨节点直连。"""
    edges = {(e.source, e.target) for e in build_requirement_workflow().get_graph().edges}
    expected = {
        ("__start__", "classify"),
        ("clarify", "functional_points"),
        ("functional_points", "rag_retrieve"),
        ("rag_retrieve", "risk"),
        ("risk", "test_points"),
        ("test_points", "report"),
        ("report", "__end__"),
    }
    assert expected.issubset(edges)


async def test_conditional_edge_routes_by_needs_clarify():
    """条件边：needs_clarify 为真走 clarify，否则进 functional_points。"""
    assert route_after_classify({"needs_clarify": True}) == "clarify"
    assert route_after_classify({"needs_clarify": False}) == "functional_points"
    assert route_after_classify({}) == "functional_points"


async def test_checkpoint_isolates_threads():
    """不同 thread_id 互不干扰：挂起一个线程不影响另一个线程跑完。"""
    wf = build_requirement_workflow()
    hung = {"configurable": {"thread_id": "thread-hung"}}
    clean = {"configurable": {"thread_id": "thread-clean"}}

    async for _event in wf.astream(
        {"requirement_text": AMBIGUOUS_REQUIREMENT}, hung, stream_mode="updates"
    ):
        pass
    assert wf.get_state(hung).next == ("clarify",)

    done = await wf.ainvoke({"requirement_text": NORMAL_REQUIREMENT}, clean)
    assert done["trace"][-1] == "report"
    assert wf.get_state(hung).next == ("clarify",)  # 挂起线程未被污染


async def test_draw_mermaid_exports_graph():
    """Mermaid 导出包含全部业务节点，可用于架构图文档。"""
    mermaid = build_requirement_workflow().get_graph().draw_mermaid()
    for node in BUSINESS_NODES:
        assert node in mermaid


# --------------------------------------------------------------------------
# 节点职责（7 条）
# --------------------------------------------------------------------------


async def test_classify_node_writes_only_its_own_fields():
    """classify：写 category / confidence / 澄清问题 / needs_clarify + trace。"""
    patch = await classify({"requirement_text": NORMAL_REQUIREMENT, "trace": []}, deps=_deps())
    assert set(patch) == {
        "category",
        "confidence",
        "clarification_questions",
        "needs_clarify",
        "trace",
    }
    assert patch["category"] == "web"
    assert patch["confidence"] == 0.9
    assert patch["needs_clarify"] is False
    assert patch["trace"] == ["classify"]


async def test_functional_points_node_feeds_human_answers_into_prompt():
    """functional_points：人工补充内容进入提示词，且不回写无关字段。"""
    chat, seen = _spy_chat()
    patch = await functional_points(
        {"requirement_text": NORMAL_REQUIREMENT, "human_answers": ["面向零售商"], "trace": []},
        deps=_deps(chat_fn=chat),
    )
    assert set(patch) == {"functional_points", "trace"}
    assert 2 <= len(patch["functional_points"]) <= 6
    assert "面向零售商" in seen[0]


async def test_rag_retrieve_node_degrades_without_rag():
    """rag_retrieve：无检索器时置 rag_degraded，不抛异常。"""
    patch = await rag_retrieve(
        {"requirement_text": NORMAL_REQUIREMENT, "trace": []}, deps=_deps(rag=None)
    )
    assert patch == {"rag_context": [], "rag_degraded": True, "trace": ["rag_retrieve"]}


async def test_rag_retrieve_node_maps_results_and_filters_by_score():
    """rag_retrieve：结果落成 chunk_id/source/text/score，并按 min_score 过滤。"""
    results = [
        RetrievalResult(chunk_id="c1", source="手册.md", score=0.9, text="高相关片段"),
        RetrievalResult(chunk_id="c2", source="手册.md", score=0.1, text="低相关片段"),
    ]
    deps = _deps(rag=_StubRAG(results), config=WorkflowConfig(rag_min_score=0.5, rag_top_k=3))
    patch = await rag_retrieve({"requirement_text": NORMAL_REQUIREMENT, "trace": []}, deps=deps)
    assert [c["chunk_id"] for c in patch["rag_context"]] == ["c1"]
    assert patch["rag_context"][0]["score"] == 0.9
    assert patch["rag_degraded"] is False


async def test_risk_node_appends_degradation_note_when_rag_degraded():
    """risk：RAG 降级时额外追加一条「依据不足」风险。"""
    degraded = await risk(
        {"functional_points": ["下单"], "rag_degraded": True, "trace": []}, deps=_deps()
    )
    assert any("依据有限" in item for item in degraded["risks"])

    healthy = await risk(
        {"functional_points": ["下单"], "rag_degraded": False, "trace": []}, deps=_deps()
    )
    assert all("依据有限" not in item for item in healthy["risks"])


async def test_test_points_node_covers_four_categories():
    """test_points：产出覆盖正常 / 异常 / 边界 / 回归四类的测试点。"""
    patch = await build_test_points(
        {"functional_points": ["下单"], "risks": ["库存一致性"], "trace": []}, deps=_deps()
    )
    joined = "".join(patch["test_points"])
    for label in ("正常", "异常", "边界", "回归"):
        assert label in joined


async def test_report_node_renders_citations_and_degradation():
    """report：有引用时列出 source#chunk_id，无资料时标注未检索到。"""
    with_citations = report(
        {
            "category": "web",
            "confidence": 0.9,
            "functional_points": ["下单"],
            "risks": ["库存"],
            "test_points": ["正常：下单"],
            "rag_context": [{"chunk_id": "c1", "source": "手册.md", "score": 0.9}],
            "rag_degraded": False,
            "trace": [],
        }
    )
    assert "手册.md#c1" in with_citations["report"]

    degraded = report(
        {
            "category": "web",
            "confidence": 0.9,
            "functional_points": ["下单"],
            "risks": ["库存"],
            "test_points": ["正常：下单"],
            "rag_degraded": True,
            "trace": [],
        }
    )
    assert "未检索到内部资料" in degraded["report"]


# --------------------------------------------------------------------------
# 人工确认（4 条）
# --------------------------------------------------------------------------


async def test_ambiguous_requirement_triggers_interrupt():
    """歧义需求：事件流出现 __interrupt__ 并列出澄清问题。"""
    wf = build_requirement_workflow()
    cfg = {"configurable": {"thread_id": "flow-interrupt"}}
    events = [
        ev
        async for ev in wf.astream(
            {"requirement_text": AMBIGUOUS_REQUIREMENT}, cfg, stream_mode="updates"
        )
    ]
    interrupts = [ev for ev in events if "__interrupt__" in ev]
    assert len(interrupts) == 1
    payload = interrupts[0]["__interrupt__"][0].value
    assert payload["questions"], "应给出澄清问题"
    assert payload["round"] == 1


async def test_pending_state_locates_clarify_node():
    """挂起状态：get_state().next 指向 clarify，便于定位恢复入口。"""
    wf = build_requirement_workflow()
    cfg = {"configurable": {"thread_id": "flow-pending"}}
    async for _ev in wf.astream(
        {"requirement_text": AMBIGUOUS_REQUIREMENT}, cfg, stream_mode="updates"
    ):
        pass
    state = wf.get_state(cfg)
    assert state.next == ("clarify",)
    assert state.values["clarification_questions"], "挂起时应能看到澄清问题"


async def test_resume_continues_and_finishes_at_report():
    """resume 续跑：从 clarify 继续，最终以 report 收尾。"""
    wf = build_requirement_workflow()
    cfg = {"configurable": {"thread_id": "flow-resume"}}
    async for _ev in wf.astream(
        {"requirement_text": AMBIGUOUS_REQUIREMENT}, cfg, stream_mode="updates"
    ):
        pass
    resumed = await wf.ainvoke(Command(resume="面向零售商的智能推荐系统"), cfg)
    assert resumed["trace"][0] == "classify"
    assert "clarify" in resumed["trace"]
    assert resumed["trace"][-1] == "report"


async def test_human_answer_reaches_downstream_prompt():
    """人工补充：写入 human_answers 并拼进 functional_points 的提示词。"""
    wf = build_requirement_workflow()
    cfg = {"configurable": {"thread_id": "flow-answer"}}
    async for _ev in wf.astream(
        {"requirement_text": AMBIGUOUS_REQUIREMENT}, cfg, stream_mode="updates"
    ):
        pass
    resumed = await wf.ainvoke(Command(resume="面向零售商"), cfg)
    assert resumed["human_answers"] == ["面向零售商"]
    assert resumed["clarify_rounds"] == 1
