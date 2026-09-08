"""RequirementAnalysisWorkflow 冒烟测试：正常流程 / 人工确认 / 降级韧性。

运行（Git Bash）：
    uv run pytest tests/graph/test_workflow_smoke.py -v

覆盖路线图第八周「可观察、可重试、可恢复」三条验收线：
  - 图结构：6 个业务节点按序执行，各写各的字段（不堆在单节点）。
  - 人工确认：歧义触发 interrupt，get_state().next 可定位，Command(resume) 续跑。
  - 韧性：LLM 永久失败时图不崩溃，降级产出 + errors 记录，路径仍跑满 6 节点。
"""

from __future__ import annotations

from langgraph.types import Command

from graph.workflow import build_requirement_workflow

NORMAL_REQUIREMENT = (
    "我们公司要做一个面向中小型零售商的 B2C 电商网站，"
    "支持登录、商品浏览与搜索、购物车、订单管理、支付、后台管理。"
)


def _failing_chat():
    """构造一个永远抛错的 chat_fn，用于验证降级韧性。"""

    async def _chat(_messages):
        raise RuntimeError("LLM 服务不可用")

    return _chat


async def test_normal_flow_writes_all_fields():
    """正常需求：6 节点按序执行，各自写满负责字段。"""
    wf = build_requirement_workflow()
    res = await wf.ainvoke(
        {"requirement_text": NORMAL_REQUIREMENT},
        {"configurable": {"thread_id": "smoke-normal"}},
    )
    assert res["trace"] == [
        "classify",
        "functional_points",
        "rag_retrieve",
        "risk",
        "test_points",
        "report",
    ]
    assert res["category"] == "web"
    assert 0.0 <= res["confidence"] <= 1.0
    assert res["functional_points"], "功能点为空"
    assert res["risks"], "风险点为空"
    assert res["test_points"], "测试点为空"
    assert res["report"].startswith("# 需求分析报告")
    # 未接 RAG -> 符合设计的降级标记
    assert res["rag_degraded"] is True
    # 无节点失败则 errors 不写入状态（用 get 兼容缺省）
    assert res.get("errors", []) == []


async def test_ambiguity_interrupt_then_resume():
    """歧义需求：触发 interrupt 挂起，人工补充后从 clarify 续跑至 report。"""
    wf = build_requirement_workflow()
    cfg = {"configurable": {"thread_id": "smoke-clarify"}}

    events = [
        ev
        async for ev in wf.astream({"requirement_text": "我想做个东西"}, cfg, stream_mode="updates")
    ]
    assert any("__interrupt__" in ev for ev in events), "应出现 __interrupt__ 事件"

    state = wf.get_state(cfg)
    assert state.next == ("clarify",), "挂起节点应为 clarify"

    resumed = await wf.ainvoke(Command(resume="面向零售商的智能推荐系统"), cfg)
    assert "clarify" in resumed["trace"]
    assert resumed["human_answers"] == ["面向零售商的智能推荐系统"]
    assert resumed["trace"][-1] == "report"


async def test_fallback_records_errors_on_permanent_failure():
    """LLM 永久失败：图不崩溃，降级产出且 errors 记录所有失败节点。"""
    wf = build_requirement_workflow(chat_fn=_failing_chat())
    res = await wf.ainvoke(
        {"requirement_text": NORMAL_REQUIREMENT},
        {"configurable": {"thread_id": "smoke-fallback"}},
    )
    # 即便全部 LLM 节点失败，图仍跑满 6 节点
    assert res["trace"] == [
        "classify",
        "functional_points",
        "rag_retrieve",
        "risk",
        "test_points",
        "report",
    ]
    assert res["errors"], "应记录节点失败"
    assert res["category"] == "other"  # 分类降级
    assert res["functional_points"] == []  # 功能点降级为空
    assert res["report"].startswith("# 需求分析报告")  # 报告仍生成
