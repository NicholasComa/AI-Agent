"""本地 JSONL 后端用例：落盘格式、读回容错、不可写时的降级。

本地后端是默认后端，也是离线环境的唯一去处，因此它必须满足两条：文件能被人
直接看（逐行 JSON、中文不转义），以及文件被改坏时读回不能中断。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from observability_fakes import (
    make_config,
    make_jsonl_backend,
    make_tracer,
    read_lines,
    trace_files,
)

from observability import BackendUnavailableError, JsonlBackend, SpanRecord, Tracer, build_backend


def test_writes_daily_file_with_full_schema(tmp_path: Path) -> None:
    """按天切片落盘，每行都是字段齐全的 JSON，且父子关系可复原。"""
    tracer = make_tracer(tmp_path)
    with (
        tracer.trace("request", seed="flat", note="检索"),
        tracer.span("retrieve", kind="retriever", top_k=3),
    ):
        pass
    tracer.flush()

    directory = tmp_path / "traces"
    files = trace_files(directory)
    assert len(files) == 1
    assert files[0].name == f"traces-{datetime.now(UTC):%Y-%m-%d}.jsonl"

    raw = files[0].read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert raw.count("\n") == 2
    assert "检索" in raw, "中文应保持可读，不被转义成 \\uXXXX"

    payloads = read_lines(directory)
    assert len(payloads) == 2
    expected_keys = set(SpanRecord(trace_id="t", span_id="s").to_payload())
    assert set(payloads[0]) == expected_keys
    assert set(payloads[1]) == expected_keys

    root = next(item for item in payloads if item["kind"] == "trace")
    child = next(item for item in payloads if item["kind"] == "retriever")
    assert root["parent_id"] is None
    assert root["attributes"]["note"] == "检索"
    assert child["parent_id"] == root["span_id"]
    assert child["trace_id"] == root["trace_id"]
    assert child["attributes"] == {"top_k": 3}
    assert child["status"] == "ok"
    assert child["ended_at"] is not None
    assert child["unfinished"] is False


def test_span_payload_is_written_in_entry_order(tmp_path: Path) -> None:
    """先结束的先落盘，便于按行顺序还原执行时序。"""
    tracer = make_tracer(tmp_path)
    with tracer.trace("request"):
        with tracer.span("retrieve", kind="retriever"):
            pass
        with tracer.span("generate", kind="generation"):
            pass

    names = [record.name for record in make_jsonl_backend(tmp_path).read_records()]
    assert names == ["retrieve", "generate", "request"]


def test_read_back_skips_bad_lines(tmp_path: Path) -> None:
    """空行、非法 JSON、非字典 JSON 都跳过；结构不全的记录还原时丢弃。"""
    tracer = make_tracer(tmp_path)
    with tracer.trace("request", seed="flat"):
        pass
    tracer.flush()

    target = trace_files(tmp_path / "traces")[0]
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write("{this is not json}\n")
        handle.write("[1, 2, 3]\n")
        handle.write(json.dumps({"trace_id": "t", "name": "缺 span 标识"}) + "\n")

    backend = make_jsonl_backend(tmp_path)
    payloads = backend.read_all()
    records = backend.read_records()

    assert len(payloads) == 2
    assert len(records) == 1
    assert records[0].name == "request"
    assert records[0].duration_ms is not None


def test_probe_does_not_leave_empty_trace_file(tmp_path: Path) -> None:
    """构造后端只验证目录可写，不在零流量时留下当天的空文件。"""
    make_jsonl_backend(tmp_path)

    assert (tmp_path / "traces").is_dir()
    assert trace_files(tmp_path / "traces") == []
    assert list((tmp_path / "traces").iterdir()) == []


def test_unwritable_directory_raises(tmp_path: Path) -> None:
    """目录位置被普通文件占住时，构造即失败，而不是等到写记录才发现。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(BackendUnavailableError):
        JsonlBackend(blocker / "traces")


def test_unwritable_directory_degrades_to_memory(tmp_path: Path) -> None:
    """本地后端不可用时退到内存后端，追踪调用不抛异常，原因可查。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    config = replace(make_config(tmp_path), trace_dir=blocker / "traces")

    backend = build_backend(config)

    assert backend.name == "memory"
    assert "jsonl unavailable" in (backend.degraded_reason or "")

    tracer = Tracer(config, backend)
    with tracer.trace("request"), tracer.span("retrieve", kind="retriever"):
        pass

    assert backend.end_count == 2
    assert [payload["name"] for payload in backend.read_all()] == ["retrieve", "request"]
