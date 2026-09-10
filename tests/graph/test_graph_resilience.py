"""RequirementAnalysisWorkflow 韧性测试：重试 / 降级 / 失败可定位。

运行（Git Bash）：
    uv run pytest tests/graph/test_graph_resilience.py -v

覆盖「节点失败可定位并重试」这条验收线：瞬时错误重试后成功、永久错误写
``errors`` 并降级、依赖不可用不中断图、失败后报告仍产出、配置可控制重试次数。
"""

from __future__ import annotations

from graph.config import WorkflowConfig
from graph.fakes import fake_chat
from graph.workflow import build_requirement_workflow

NORMAL_REQUIREMENT = (
    "我们公司要做一个面向中小型零售商的 B2C 电商网站，"
    "支持登录、商品浏览与搜索、购物车、订单管理、支付、后台管理。"
)
FULL_TRACE = [
    "classify",
    "functional_points",
    "rag_retrieve",
    "risk",
    "test_points",
    "report",
]


def _flaky_chat(fail_times: int):
    """前 fail_times 次调用抛错，之后正常返回；用于模拟瞬时故障。

    抛 ``ConnectionError``：与真实链路的网络抖动一致（langgraph 内置
    RetryPolicy 也只对这类错误重试）。
    """
    calls = {"n": 0}

    async def _chat(messages):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise ConnectionError("连接被重置（瞬时故障）")
        return await fake_chat(messages)

    return _chat, calls


def _always_failing_chat():
    """持续失败的 chat_fn，模拟 LLM 服务整体不可用。"""

    async def _chat(_messages):
        raise RuntimeError("LLM 服务不可用")

    return _chat


class _BrokenRAG:
    """检索一律抛错，模拟 Qdrant 未启动。"""

    def retrieve(self, query: str, top_k: int = 3):  # noqa: ARG002
        raise ConnectionError("Qdrant 未启动")


async def test_transient_failure_retries_then_succeeds():
    """瞬时故障：重试后成功，不写 errors，图跑满 6 节点。"""
    chat, calls = _flaky_chat(fail_times=2)
    wf = build_requirement_workflow(chat_fn=chat)
    res = await wf.ainvoke(
        {"requirement_text": NORMAL_REQUIREMENT},
        {"configurable": {"thread_id": "res-transient"}},
    )
    assert res["trace"] == FULL_TRACE
    assert res.get("errors", []) == [], "瞬时故障重试成功后不应记录错误"
    assert calls["n"] > 2, "应发生重试而非首次即成功"
    assert res["category"] == "web", "重试成功后分类结果正常"


async def test_permanent_failure_records_error_entry():
    """永久失败：写 errors，条目含 node / type / message / attempts。"""
    wf = build_requirement_workflow(chat_fn=_always_failing_chat())
    res = await wf.ainvoke(
        {"requirement_text": NORMAL_REQUIREMENT},
        {"configurable": {"thread_id": "res-permanent"}},
    )
    errors = res["errors"]
    assert errors, "永久失败时应记录错误条目"
    for entry in errors:
        assert {"node", "type", "message", "attempts"} <= set(entry)
        assert entry["type"] == "RuntimeError"
        assert entry["attempts"] == 3  # 默认 max_retries


async def test_max_retries_is_configurable():
    """重试次数由 WorkflowConfig.max_retries 控制。"""
    wf = build_requirement_workflow(
        chat_fn=_always_failing_chat(), config=WorkflowConfig(max_retries=2)
    )
    res = await wf.ainvoke(
        {"requirement_text": NORMAL_REQUIREMENT},
        {"configurable": {"thread_id": "res-retries"}},
    )
    assert all(entry["attempts"] == 2 for entry in res["errors"])


async def test_rag_unavailable_does_not_break_graph():
    """依赖降级：知识库不可用只置 rag_degraded，图继续跑完。"""
    wf = build_requirement_workflow(rag=_BrokenRAG())
    res = await wf.ainvoke(
        {"requirement_text": NORMAL_REQUIREMENT},
        {"configurable": {"thread_id": "res-ragdown"}},
    )
    assert res["trace"] == FULL_TRACE
    assert res["rag_degraded"] is True
    assert res["rag_context"] == []
    assert res.get("errors", []) == [], "依赖降级不算节点失败"


async def test_report_still_generated_after_all_nodes_fail():
    """全部 LLM 节点失败后，报告仍产出，并可从 errors 定位失败节点。"""
    wf = build_requirement_workflow(chat_fn=_always_failing_chat())
    res = await wf.ainvoke(
        {"requirement_text": NORMAL_REQUIREMENT},
        {"configurable": {"thread_id": "res-report"}},
    )
    assert res["report"].startswith("# 需求分析报告")
    failed_nodes = [entry["node"] for entry in res["errors"]]
    assert failed_nodes == ["classify", "functional_points", "risk", "test_points"]
