"""需求分析工作流接口测试：正常路径、挂起续跑、流式帧与边界。"""

from __future__ import annotations

from service_fakes import AMBIGUOUS_REQUIREMENT, NORMAL_REQUIREMENT, Harness

_BASE = "/v1/workflow"
_START = f"{_BASE}/requirement-analysis"


async def test_start_completes_and_writes_report(harness: Harness) -> None:
    """完整需求 → status=completed，trace 覆盖全部业务节点。"""
    response = await harness.post(_START, {"requirement_text": NORMAL_REQUIREMENT})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["clarification_questions"] == []
    assert body["thread_id"]
    assert body["report"]
    assert body["trace"] == [
        "classify",
        "functional_points",
        "rag_retrieve",
        "risk",
        "test_points",
        "report",
    ]
    assert body["request_id"]


async def test_start_suspends_on_ambiguous_requirement(harness: Harness) -> None:
    """信息不足的需求 → 挂起并列出澄清问题，且不产出报告。

    挂起发生在 clarify 节点内部的 ``interrupt()``，该节点还没返回自己的 patch，
    因此此刻的 trace 只到 ``classify``。
    """
    response = await harness.post(_START, {"requirement_text": AMBIGUOUS_REQUIREMENT})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_clarification"
    assert len(body["clarification_questions"]) >= 1
    assert body["report"] is None
    assert body["trace"] == ["classify"]


async def test_resume_continues_from_suspension(harness: Harness) -> None:
    """补充答案后从挂起点续跑完成，trace 里保留 clarify 痕迹。"""
    started = (await harness.post(_START, {"requirement_text": AMBIGUOUS_REQUIREMENT})).json()
    response = await harness.post(
        f"{_BASE}/{started['thread_id']}/resume",
        {"answers": ["仅 Web 版，不做移动端"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["report"]
    assert body["trace"] == [
        "classify",
        "clarify",
        "functional_points",
        "rag_retrieve",
        "risk",
        "test_points",
        "report",
    ]


async def test_resume_without_pending_returns_422(harness: Harness) -> None:
    """线程没有挂起点时明确报 422，而不是让 Command(resume) 语义不明地跑下去。"""
    started = (await harness.post(_START, {"requirement_text": NORMAL_REQUIREMENT})).json()
    response = await harness.post(
        f"{_BASE}/{started['thread_id']}/resume",
        {"answers": ["补充"]},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_argument"
    assert "no pending clarification" in error["message"]


async def test_resume_unknown_thread_returns_422(harness: Harness) -> None:
    """从未出现过的线程同样按「无挂起点」处理。"""
    response = await harness.post(f"{_BASE}/deadbeef/resume", {"answers": ["补充"]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_argument"


async def test_stream_emits_node_frames_then_done(harness: Harness) -> None:
    """流式：每个节点一帧 node，最后 done 带完整结果。"""
    frames = await harness.frames(_START, {"requirement_text": NORMAL_REQUIREMENT, "stream": True})
    kinds = [frame["type"] for frame in frames]
    assert kinds[:-1] == ["node"] * len(kinds[:-1])
    assert kinds[-1] == "done"
    assert [frame["node"] for frame in frames[:-1]] == [
        "classify",
        "functional_points",
        "rag_retrieve",
        "risk",
        "test_points",
        "report",
    ]
    assert frames[-1]["status"] == "completed"
    assert frames[-1]["report"]


async def test_stream_emits_interrupt_frame(harness: Harness) -> None:
    """流式遇到挂起时推 interrupt 帧，并在 done 里给出澄清问题。"""
    frames = await harness.frames(
        _START, {"requirement_text": AMBIGUOUS_REQUIREMENT, "stream": True}
    )
    kinds = [frame["type"] for frame in frames]
    assert "interrupt" in kinds
    interrupt = frames[kinds.index("interrupt")]
    assert len(interrupt["questions"]) >= 1
    assert frames[-1]["type"] == "done"
    assert frames[-1]["status"] == "awaiting_clarification"


async def test_stream_resume_completes_after_clarification(harness: Harness) -> None:
    """流式续跑：从 clarify 之后继续，最后 done 里带完整报告。"""
    started = (await harness.post(_START, {"requirement_text": AMBIGUOUS_REQUIREMENT})).json()
    frames = await harness.frames(
        f"{_BASE}/{started['thread_id']}/resume",
        {"answers": ["仅 Web 版，不做移动端"], "stream": True},
    )
    assert frames[-1]["type"] == "done"
    assert frames[-1]["status"] == "completed"
    assert frames[-1]["report"]


async def test_session_binding_reuses_thread_for_resume(harness: Harness) -> None:
    """带 session_id 启动后，会话记录里绑定同一个线程，轮次被累加。"""
    body = (
        await harness.post(
            _START,
            {"requirement_text": AMBIGUOUS_REQUIREMENT, "session_id": "sess-1"},
        )
    ).json()
    record = harness.deps.sessions.session("sess-1")
    assert record is not None
    assert record.thread_id == body["thread_id"]
    assert record.turns == 1
    assert body["session_id"] == "sess-1"


async def test_explicit_thread_id_wins_over_generated(harness: Harness) -> None:
    """调用方显式指定线程时以其为准，便于客户端自行管理连续性。"""
    body = (
        await harness.post(
            _START,
            {"requirement_text": NORMAL_REQUIREMENT, "thread_id": "fixed-thread"},
        )
    ).json()
    assert body["thread_id"] == "fixed-thread"


async def test_start_rejects_invalid_payload(harness: Harness) -> None:
    """空需求文本与超长会话 ID 在入口被拒。"""
    empty = await harness.post(_START, {"requirement_text": ""})
    assert empty.status_code == 422

    long_session = await harness.post(
        _START,
        {"requirement_text": NORMAL_REQUIREMENT, "session_id": "x" * 65},
    )
    assert long_session.status_code == 422


async def test_start_sets_trace_header(harness: Harness) -> None:
    """工作流成功响应带 ``X-Trace-Id``。

    头写在注入的 ``Response`` 上而不是返回模型实例：给声明式返回模型设未声明
    字段会抛 ``ValueError``，且该异常发生在 ``tracer.trace()`` 体内时会把根
    span 记成错误。
    """
    response = await harness.post(_START, {"requirement_text": NORMAL_REQUIREMENT})
    assert response.status_code == 200
    trace_id = response.headers.get("X-Trace-Id")
    assert trace_id, "工作流响应缺少 X-Trace-Id"
    assert len(trace_id) == 32


async def test_resume_sets_trace_header(harness: Harness) -> None:
    """续跑接口同样带头。"""
    started = (await harness.post(_START, {"requirement_text": AMBIGUOUS_REQUIREMENT})).json()
    response = await harness.post(
        f"{_BASE}/{started['thread_id']}/resume",
        {"answers": ["面向内部客服团队", "先做创建与流转"]},
    )
    assert response.status_code == 200
    assert response.headers.get("X-Trace-Id")


async def test_start_records_one_chain_span_per_executed_node(harness: Harness) -> None:
    """chain span 数与实际执行节点数一致，且没有把 pregel 包装层也记成节点。

    LangGraph 每个节点外层还有一层同名包装，回调会看到两层 ``on_chain_start``；
    处理器必须把包装层折叠掉，否则节点数会翻倍（6 个节点会得到 12 条 chain）。
    """
    response = await harness.post(_START, {"requirement_text": NORMAL_REQUIREMENT})
    assert response.status_code == 200
    executed = response.json()["trace"]

    rows = _read_trace_rows(harness.deps.tracer)
    chain_names = [row["name"] for row in rows if row.get("kind") == "chain"]
    assert sorted(chain_names) == sorted(executed)
    assert len(chain_names) == len(executed) == 6


def _read_trace_rows(tracer: object) -> list[dict[str, object]]:
    """把该追踪器已落盘的记录读成字典列表。"""
    import json
    from pathlib import Path

    backend = getattr(tracer, "backend", None)
    directory = getattr(backend, "directory", None)
    assert directory is not None, "测试后端应暴露落盘目录 directory"
    rows: list[dict[str, object]] = []
    for path in sorted(Path(directory).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows
