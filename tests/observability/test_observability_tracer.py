"""追踪门面用例：span 树、上下文隔离、异常与收尾、采样。

断言一律对替身后端的内存快照做，不读文件，因此这些用例既快又不依赖磁盘。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from observability_fakes import LONG_TEXT, SOURCE_NAME, RecordingBackend, make_tracer

from logging_config import request_id_var
from observability import Tracer, content_payload
from observability.models import new_trace_id
from observability.tracer import TRACE_KIND


def test_span_tree_links_parents(tracer: Tracer, recorded: RecordingBackend) -> None:
    """同一段代码里依次进入的 span 形成父子链，根 span 的父标识为空。

    层级由上下文栈推导：每进入一个 span 就把「当前 span」换成本身，退出时换回
    父节点，因此嵌套书写与并列书写得到同一棵树。
    """
    with (
        tracer.trace("request", seed="tree") as root,
        tracer.span("retrieve", kind="retriever") as retrieve,
        tracer.span("generate", kind="generation") as generate,
    ):
        pass

    trace_root = recorded.by_name("request")
    assert trace_root.kind == TRACE_KIND
    assert trace_root.parent_id is None
    assert trace_root.span_id == root.span_id
    assert retrieve.parent_id == trace_root.span_id
    assert generate.parent_id == retrieve.span_id
    assert trace_root.ended_at is not None
    assert trace_root.duration_ms is not None
    assert trace_root.duration_ms >= 0


def test_trace_id_is_shared_and_reproducible(tracer: Tracer, recorded: RecordingBackend) -> None:
    """同一条 trace 内标识一致，且相同 seed 每次得到同一个标识。"""
    with tracer.trace("request", seed="fixed-seed"):
        with tracer.span("retrieve", kind="retriever"):
            pass
        with tracer.span("generate", kind="generation"):
            pass

    trace_ids = {record.trace_id for record in recorded.records}
    assert len(trace_ids) == 1

    trace_id = next(iter(trace_ids))
    assert trace_id == new_trace_id("fixed-seed")
    assert len(trace_id) == 32
    assert int(trace_id, 16) >= 0
    assert new_trace_id("fixed-seed") == new_trace_id("fixed-seed")
    assert new_trace_id("other-seed") != new_trace_id("fixed-seed")


def test_span_error_is_recorded_and_reraised(tracer: Tracer, recorded: RecordingBackend) -> None:
    """span 内抛异常时记录错误状态，异常原样向上抛，不吞。"""
    with (
        pytest.raises(ValueError, match="boom"),
        tracer.trace("request"),
        tracer.span("retrieve", kind="retriever"),
    ):
        raise ValueError("boom \n   多行  消息")

    span = recorded.by_name("retrieve")
    assert span.status == "error"
    assert span.error_type == "ValueError"
    assert span.error_message == "boom 多行 消息"

    root = recorded.by_name("request")
    assert root.status == "error"
    assert root.error_type == "ValueError"


def test_successful_span_stays_ok(tracer: Tracer, recorded: RecordingBackend) -> None:
    """正常退出的 span 状态为 ok，且不误标错误字段。"""
    with tracer.trace("request"), tracer.span("retrieve", kind="retriever"):
        pass

    span = recorded.by_name("retrieve")
    assert span.status == "ok"
    assert span.error_type is None
    assert span.error_message is None
    assert span.unfinished is False


def test_flush_closes_unfinished_spans(tracer: Tracer, recorded: RecordingBackend) -> None:
    """漏写出口的 span 被收尾为错误并标未完成，而不是静默丢弃。"""
    context_manager = tracer.span("leaked", kind="retriever")
    context_manager.__enter__()

    tracer.flush()
    leaked = [record for record in recorded.records if record.name == "leaked"]
    tracer.flush()
    context_manager.__exit__(None, None, None)

    assert len(leaked) == 1
    assert leaked[0].status == "error"
    assert leaked[0].unfinished is True
    assert leaked[0].error_type == "UnfinishedSpan"
    assert leaked[0].ended_at is not None


def test_trace_context_is_cleared_after_exit(tracer: Tracer, recorded: RecordingBackend) -> None:
    """trace 结束后上下文复位，此后新建的 span 不会挂到已收尾的 trace 上。"""
    with tracer.trace("request", seed="first"):
        pass

    assert tracer.current_trace_id() is None
    assert tracer.current_span() is None

    with tracer.span("orphan", kind="retriever"):
        pass

    orphan = recorded.by_name("orphan")
    assert orphan.parent_id is None
    assert orphan.trace_id != new_trace_id("first")


def test_orphan_span_opens_its_own_trace(tracer: Tracer, recorded: RecordingBackend) -> None:
    """没有活动 trace 时，span 自建一条 trace 并读取上下文里的请求标识。"""
    token = request_id_var.set("req-abc123")
    try:
        with tracer.span("solo", kind="generation"):
            pass
    finally:
        request_id_var.reset(token)

    solo = recorded.by_name("solo")
    assert solo.parent_id is None
    assert solo.request_id == "req-abc123"
    assert len(solo.trace_id) == 32
    assert solo.kind == "generation"


def test_unknown_span_kind_falls_back(tracer: Tracer, recorded: RecordingBackend) -> None:
    """未登记的 kind 回退为 span，不抛异常。"""
    with tracer.trace("request"), tracer.span("odd", kind="not-a-kind"):
        pass

    assert recorded.by_name("odd").kind == "span"


def test_span_attributes_are_sanitized(tmp_path: Path, recorded: RecordingBackend) -> None:
    """元数据只留合规标量：敏感键、超长字符串、嵌套结构一律丢弃。"""
    tracer = make_tracer(tmp_path, backend=recorded, max_label_chars=32)
    with (
        tracer.trace("request"),
        tracer.span(
            "retrieve",
            kind="retriever",
            top_k=3,
            top1_score=0.83,
            cache_hit=False,
            stages=["chunk", "score"],
            prompt=LONG_TEXT,
            source=SOURCE_NAME,
            note="x" * 200,
            payload={"nested": "value"},
        ),
    ):
        pass

    attributes = recorded.by_name("retrieve").attributes
    assert attributes == {
        "top_k": 3,
        "top1_score": 0.83,
        "cache_hit": False,
        "stages": ["chunk", "score"],
    }


def test_usage_and_cost_are_recorded(tracer: Tracer, recorded: RecordingBackend) -> None:
    """model 与 token 用量写入记录，本地模型成本按 0 折算而不是留空。"""
    with tracer.trace("request"), tracer.span("generate", kind="generation"):
        assert tracer.set_usage(model="qwen3:1.7b", input_tokens=120, output_tokens=30)

    generate = recorded.by_name("generate")
    assert generate.model == "qwen3:1.7b"
    assert generate.usage == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    assert generate.cost_usd == 0.0


def test_unknown_model_leaves_cost_empty(tracer: Tracer, recorded: RecordingBackend) -> None:
    """单价表里没有的模型，成本留空而不是填 0——「免费」与「不知道单价」不同。"""
    with tracer.trace("request"), tracer.span("generate", kind="generation"):
        tracer.set_usage(model="some-cloud-model", input_tokens=10, output_tokens=5)

    assert recorded.by_name("generate").cost_usd is None


def test_span_without_active_span_is_a_noop(tracer: Tracer, recorded: RecordingBackend) -> None:
    """没有活动 span 时标记错误与写属性都返回假，不抛异常。"""
    assert tracer.mark_error("ValueError", "no active span") is False
    assert tracer.set_attributes(top_k=1) is False
    assert tracer.set_usage(model="qwen3", input_tokens=1, output_tokens=1) is False
    assert recorded.records == []


def test_sample_rate_zero_dispatches_nothing(tmp_path: Path, recorded: RecordingBackend) -> None:
    """采样率为 0 时不投递任何记录，但 trace 标识照常生成。"""
    tracer = make_tracer(tmp_path, backend=recorded, sample_rate=0.0)

    with (
        tracer.trace("request", seed="dropped") as root,
        tracer.span("retrieve", kind="retriever"),
    ):
        pass

    assert recorded.records == []
    assert recorded.begun == []
    assert root.trace_id == new_trace_id("dropped")


def test_disabled_tracer_dispatches_nothing(tmp_path: Path, recorded: RecordingBackend) -> None:
    """总开关关闭时同样零投递，用于「需要静默运行」的场景。"""
    tracer = make_tracer(tmp_path, backend=recorded, enabled=False)

    with tracer.trace("request"), tracer.span("retrieve", kind="retriever"):
        pass

    assert recorded.records == []
    assert recorded.begun == []


def test_payload_never_contains_raw_text(tracer: Tracer, recorded: RecordingBackend) -> None:
    """未开启内容捕获时，正文与来源文件名都不得出现在载荷里。"""
    with tracer.trace("request"), tracer.span("retrieve", kind="retriever", source=SOURCE_NAME):
        tracer.set_attributes(prompt=LONG_TEXT, answer=LONG_TEXT)
        with tracer.span("generate", kind="generation") as generate:
            generate.content = content_payload(LONG_TEXT, capture=False, max_chars=2000)

    payload = recorded.payload()
    assert LONG_TEXT[:20] not in payload
    assert SOURCE_NAME not in payload
    assert "sha256" in payload
    assert recorded.by_name("generate").content is not None
    assert "text" not in (recorded.by_name("generate").content or {})


def test_content_capture_is_truncated(tmp_path: Path, recorded: RecordingBackend) -> None:
    """开启捕获后原文才出现，且按上限截断；哈希始终基于完整正文。"""
    tracer = make_tracer(tmp_path, backend=recorded, capture_content=True, max_content_chars=16)
    with tracer.trace("request"), tracer.span("generate", kind="generation") as generate:
        generate.content = content_payload(
            LONG_TEXT, capture=True, max_chars=tracer.config.max_content_chars
        )

    content = recorded.by_name("generate").content or {}
    assert content["text"] == LONG_TEXT[:16]
    assert content["truncated"] is True
    assert content["chars"] == len(LONG_TEXT)
    assert content["sha256"] == content_payload(LONG_TEXT, capture=False, max_chars=16)["sha256"]


async def test_context_is_isolated_between_tasks(
    tracer: Tracer, recorded: RecordingBackend
) -> None:
    """并发任务各持一份上下文，父节点与 trace 标识互不串台。

    两个任务在同一处让出事件循环，因此若「当前 span」存成追踪器实例上的列表，
    后启动的任务会把先启动任务的 span 认成父节点，这里就会失败。
    """

    async def run(seed: str, name: str) -> str:
        with tracer.trace("request", seed=seed):
            await asyncio.sleep(0)
            with tracer.span(name, kind="retriever"):
                await asyncio.sleep(0)
                return tracer.current_trace_id() or ""

    first_id, second_id = await asyncio.gather(run("alpha", "alpha-span"), run("beta", "beta-span"))

    assert first_id == new_trace_id("alpha")
    assert second_id == new_trace_id("beta")
    assert first_id != second_id

    alpha = recorded.by_name("alpha-span")
    beta = recorded.by_name("beta-span")
    assert alpha.trace_id == first_id
    assert beta.trace_id == second_id
    assert alpha.parent_id is not None
    assert beta.parent_id is not None
    assert alpha.parent_id != beta.parent_id
