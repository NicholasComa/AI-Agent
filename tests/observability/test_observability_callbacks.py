"""``ObservabilityCallbackHandler`` 的用例。

这个模块的用例刻意**不依赖 LangGraph 的真实运行**：回调的入参形态（``run_id``、
``parent_run_id``、``metadata``）已经用探针在真实图上验证过并固化在这里，用它
直接驱动处理器，可以让每条用例只验证一条规则，跑得也快。

真实图上的端到端验证放在最后两条：它们断言「一条线性工作流产出的 chain span 数
等于执行过的节点数」——这正是本周的验收口径。
"""

from __future__ import annotations

from observability import ObservabilityCallbackHandler

NODE_META = {
    "langgraph_node": "classify",
    "langgraph_path": ("__pregel_pull", "classify"),
    "langgraph_step": 1,
}
"""真实回调里节点元数据的关键字段（其余字段对判定无影响）。"""


def _start(handler, run_id: str, node: str | None, parent: str | None = None, inputs=None):
    """驱动一次 ``on_chain_start``。"""
    metadata = dict(NODE_META)
    if node is None:
        metadata = {"ls_integration": "x", "thread_id": "t"}
    else:
        metadata["langgraph_node"] = node
        metadata["langgraph_path"] = ("__pregel_pull", node)
    handler.on_chain_start(
        None,
        inputs if inputs is not None else {"requirement_text": "x"},
        run_id=run_id,
        parent_run_id=parent,
        metadata=metadata,
    )


def _end(handler, run_id: str, outputs=None, parent: str | None = None):
    """驱动一次 ``on_chain_end``。"""
    handler.on_chain_end(
        outputs if outputs is not None else {}, run_id=run_id, parent_run_id=parent
    )


# ----------------------------------------------------------------------
# 节点识别
# ----------------------------------------------------------------------


def test_anonymous_root_is_ignored(tracer, recorded):
    """匿名根（没有 langgraph_node）不进 trace。"""
    handler = ObservabilityCallbackHandler(tracer)

    _start(handler, "root", None)

    assert recorded.records == []
    assert handler.open_count == 0


def test_node_span_uses_chain_kind(tracer, recorded):
    """节点记录类型固定为 ``chain``。"""
    handler = ObservabilityCallbackHandler(tracer)

    _start(handler, "n1", "classify")
    _end(handler, "n1")

    assert recorded.by_name("classify").kind == "chain"


def test_node_name_comes_from_metadata(tracer, recorded):
    """节点名取自元数据，而不是 ``serialized``。"""
    handler = ObservabilityCallbackHandler(tracer)

    _start(handler, "n1", "rag_retrieve")
    _end(handler, "n1")

    assert recorded.by_name("rag_retrieve").name == "rag_retrieve"


def test_metadata_without_path_is_treated_as_anonymous(tracer, recorded):
    """只有 ``langgraph_node`` 而缺 ``langgraph_path`` 时按匿名处理。"""
    handler = ObservabilityCallbackHandler(tracer)

    handler.on_chain_start(None, {}, run_id="n1", metadata={"langgraph_node": "classify"})

    assert recorded.records == []


def test_input_keys_are_recorded_without_values(tracer, recorded):
    """记录输入字段名，但不落字段值。"""
    handler = ObservabilityCallbackHandler(tracer)

    _start(handler, "n1", "classify", inputs={"requirement_text": "内部需求原文", "trace": []})
    _end(handler, "n1")

    span = recorded.by_name("classify")
    assert span.attributes["input_keys"] == ["requirement_text", "trace"]
    assert "内部需求原文" not in recorded.payload()


# ----------------------------------------------------------------------
# 父子关系
# ----------------------------------------------------------------------


def test_span_attaches_to_active_trace_root(tracer, recorded):
    """请求级 trace 已建立时，节点挂在根 span 下。"""
    handler = ObservabilityCallbackHandler(tracer)

    with tracer.trace("workflow.start") as root:
        _start(handler, "n1", "classify")
        _end(handler, "n1")

    assert recorded.by_name("classify").parent_id == root.span_id


def test_creates_root_when_no_active_trace(tracer, recorded):
    """没有活动 trace 时自补一条根，且所有节点共用同一 trace 标识。

    根 span 由 ``start_trace`` 建立、需 ``end_trace`` 收尾；这里不调收尾，因此
    不会出现 ``kind="trace"`` 的**已结束**记录——断言落在「所有节点同属一条
    trace」这个更实质的性质上。
    """
    handler = ObservabilityCallbackHandler(tracer, root_name="workflow")

    _start(handler, "n1", "classify")
    _end(handler, "n1", outputs="functional_points")
    _start(handler, "n2", "risk")
    _end(handler, "n2")

    chain_spans = [span for span in recorded.records if span.kind == "chain"]
    assert [span.name for span in chain_spans] == ["classify", "risk"]
    assert len({span.trace_id for span in chain_spans}) == 1


def test_same_name_nested_wrapper_is_collapsed(tracer, recorded):
    """同名嵌套只保留最内层，span 数不因包装层翻倍。"""
    handler = ObservabilityCallbackHandler(tracer)

    with tracer.trace("root"):
        _start(handler, "wrap", "classify", inputs={"requirement_text": "x"})
        _start(handler, "real", "classify", parent="wrap", inputs={"category": "web"})
        _end(handler, "real", outputs="functional_points")
        _end(handler, "wrap", outputs={"needs_clarify": False})

    names = [span.name for span in recorded.records if span.kind == "chain"]
    assert names.count("classify") == 1


def test_collapsed_node_keeps_tree_root_parent(tracer, recorded):
    """折叠后真实节点仍挂在根上，而不是认一个已被丢弃的记录当父。"""
    handler = ObservabilityCallbackHandler(tracer)

    with tracer.trace("root") as root:
        _start(handler, "wrap", "classify", inputs={"requirement_text": "x"})
        _start(handler, "real", "classify", parent="wrap", inputs={"category": "web"})
        _end(handler, "real", outputs="functional_points")
        _end(handler, "wrap", outputs={"needs_clarify": False})

    assert recorded.by_name("classify").parent_id == root.span_id


# ----------------------------------------------------------------------
# 路线与重试
# ----------------------------------------------------------------------


def test_route_taken_from_node_string_result(tracer, recorded):
    """内层真实节点返回分支名时直接作为路线。"""
    handler = ObservabilityCallbackHandler(tracer)

    with tracer.trace("root"):
        _start(handler, "wrap", "classify")
        _start(handler, "real", "classify", parent="wrap")
        _end(handler, "real", outputs="clarify")
        _end(handler, "wrap", outputs={"needs_clarify": True})

    assert recorded.by_name("classify").attributes["route_taken"] == "clarify"


def test_route_taken_from_wrapper_state(tracer, recorded):
    """内层没给出路线时，由外层的 ``needs_clarify`` 补上。"""
    handler = ObservabilityCallbackHandler(tracer)

    with tracer.trace("root"):
        _start(handler, "wrap", "classify")
        _start(handler, "real", "classify", parent="wrap")
        _end(handler, "real", outputs={})
        _end(handler, "wrap", outputs={"needs_clarify": False})

    assert recorded.by_name("classify").attributes["route_taken"] == "functional_points"


def test_non_route_node_has_no_route_taken(tracer, recorded):
    """非判定节点不写 ``route_taken``。"""
    handler = ObservabilityCallbackHandler(tracer)

    _start(handler, "n1", "risk")
    _end(handler, "n1", outputs={"risks": []})

    assert "route_taken" not in recorded.by_name("risk").attributes


def test_retry_count_from_errors(tracer, recorded):
    """重试次数从 ``errors`` 的 ``attempts`` 里取最大值。"""
    handler = ObservabilityCallbackHandler(tracer)

    _start(handler, "n1", "risk")
    _end(handler, "n1", outputs={"errors": [{"attempts": 1}, {"attempts": 3}]})

    assert recorded.by_name("risk").attributes["retry_count"] == 3


def test_retry_count_absent_without_errors(tracer, recorded):
    """没有 ``errors`` 时不写 ``retry_count``。"""
    handler = ObservabilityCallbackHandler(tracer)

    _start(handler, "n1", "risk")
    _end(handler, "n1", outputs={"risks": []})

    assert "retry_count" not in recorded.by_name("risk").attributes


# ----------------------------------------------------------------------
# 异常与收尾
# ----------------------------------------------------------------------


def test_error_marks_span_and_keeps_message(tracer, recorded):
    """节点抛异常时标错误、写异常类型，并把多行消息压成单行。"""
    handler = ObservabilityCallbackHandler(tracer)

    _start(handler, "n1", "risk")
    handler.on_chain_error(RuntimeError("模型超时\n堆栈第二行"), run_id="n1")

    span = recorded.by_name("risk")
    assert span.status == "error"
    assert span.error_type == "RuntimeError"
    # single_line 把换行折叠成空格：多行异常消息不应在记录里换行。
    assert "\n" not in span.error_message
    assert span.error_message == "模型超时 堆栈第二行"


def test_error_for_unknown_run_is_ignored(tracer, recorded):
    """找不到对应进入记录的错误事件安静跳过，不抛异常。"""
    handler = ObservabilityCallbackHandler(tracer)

    handler.on_chain_error(RuntimeError("x"), run_id="missing")

    assert recorded.records == []


def test_duplicate_start_keeps_first_record(tracer, recorded):
    """同一 ``run_id`` 重复进入只保留最早一条。"""
    handler = ObservabilityCallbackHandler(tracer)

    _start(handler, "n1", "classify")
    _start(handler, "n1", "classify")
    _end(handler, "n1")

    assert len([span for span in recorded.records if span.name == "classify"]) == 1


def test_close_flushes_unfinished_nodes(tracer, recorded):
    """未收尾的节点在 ``close`` 时标 ``unfinished`` 并投递。"""
    handler = ObservabilityCallbackHandler(tracer)

    _start(handler, "n1", "clarify")
    handler.close()

    span = recorded.by_name("clarify")
    assert span.unfinished is True
    assert span.status == "error"
    assert span.error_type == "UnfinishedNode"
    assert handler.open_count == 0


def test_close_is_idempotent(tracer, recorded):
    """重复 ``close`` 不产生重复记录。"""
    handler = ObservabilityCallbackHandler(tracer)

    _start(handler, "n1", "clarify")
    handler.close()
    handler.close()

    assert len([span for span in recorded.records if span.name == "clarify"]) == 1


def test_close_flushes_pending_route_record(tracer, recorded):
    """暂存中的路由记录也必须在 ``close`` 时投递，不能被静默丢掉。"""
    handler = ObservabilityCallbackHandler(tracer)

    with tracer.trace("root"):
        _start(handler, "wrap", "classify")
        _start(handler, "real", "classify", parent="wrap")
        _end(handler, "real", outputs={})
        handler.close()

    assert recorded.by_name("classify").attributes["route_taken"] == "unknown"


# ----------------------------------------------------------------------
# 真实图上的端到端验证
# ----------------------------------------------------------------------


def _run_workflow(tracer, *, payload: dict) -> None:
    """在一条 trace 上跑完整工作流（用 Fake 对话函数，不接模型）。"""
    from graph.workflow import build_requirement_workflow

    class FakeChat:
        async def __call__(self, messages):
            return '{"category": "web", "confidence": 0.8, "clarification_questions": []}'

    async def _drive() -> None:
        handler = ObservabilityCallbackHandler(tracer, root_name="workflow")
        graph = build_requirement_workflow(chat_fn=FakeChat(), rag=None)
        with tracer.trace("workflow.start"):
            async for _ in graph.astream(
                payload,
                config={"configurable": {"thread_id": "t"}, "callbacks": [handler]},
            ):
                pass
            handler.close()

    import asyncio

    asyncio.run(_drive())


def test_chain_span_count_matches_executed_nodes(tracer, recorded):
    """线性路径的 chain span 数等于实际执行过的节点数（6 个）。

    图共 7 个节点，``clarify`` 只在歧义分支上执行，线性路径因此是 6 个。
    这条用例把「数不对」的常见原因（包装层翻倍、匿名根混入）都挡住了。
    """
    _run_workflow(tracer, payload={"requirement_text": "做一个电商网站"})

    chain_names = [span.name for span in recorded.records if span.kind == "chain"]
    assert chain_names == [
        "classify",
        "functional_points",
        "rag_retrieve",
        "risk",
        "test_points",
        "report",
    ]


def test_workflow_nodes_form_single_tree(tracer, recorded):
    """所有节点落在同一条 trace 且形成单根树。"""
    _run_workflow(tracer, payload={"requirement_text": "做一个电商网站"})

    chains = [span for span in recorded.records if span.kind == "chain"]
    roots = [span for span in recorded.records if span.kind == "trace"]
    assert len(roots) == 1
    assert {span.trace_id for span in recorded.records} == {roots[0].trace_id}
    assert {span.parent_id for span in chains} == {roots[0].span_id}
