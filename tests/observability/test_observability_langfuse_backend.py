"""远端上报后端用例：降级链、键名净化、不上传正文。

远端 SDK 的出错方式特别难排查——它「不上报」时不一定「抛异常」。因此降级判定
必须是显式的，这里的用例逐条固定三种降级路径，避免将来被改回「靠异常兜底」。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from observability_fakes import LONG_TEXT, FakeLangfuseClient, make_config, read_lines

import observability.backends as backends_module
from observability import Tracer, build_backend, content_payload
from observability.backends.langfuse import LangfuseBackend, to_langfuse_metadata
from observability.models import new_trace_id


def test_local_backend_has_no_degradation(tmp_path: Path) -> None:
    """未选远端时不产生降级原因，避免把「按需不启用」也报成异常。"""
    backend = build_backend(make_config(tmp_path, backend="local"))

    assert backend.name == "jsonl"
    assert backend.degraded_reason is None


def test_disabled_switch_uses_memory_backend(tmp_path: Path) -> None:
    """总开关关闭时用内存后端，零落盘、零上报。"""
    backend = build_backend(make_config(tmp_path, enabled=False))

    assert backend.name == "memory"
    assert "disabled" in (backend.degraded_reason or "")


def test_missing_sdk_degrades_to_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK 不可导入时退回本地后端，原因写明是 SDK 缺失。"""
    monkeypatch.setattr(backends_module, "langfuse_sdk_available", lambda: False)
    config = make_config(
        tmp_path,
        backend="langfuse",
        langfuse_public_key="pk-lf-demo",
        langfuse_secret_key="sk-lf-demo",
    )

    backend = build_backend(config)

    assert backend.name == "jsonl"
    assert "sdk not installed" in (backend.degraded_reason or "")


def test_missing_credentials_degrades_to_local(tmp_path: Path) -> None:
    """凭据不全时退回本地后端，原因写明是凭据缺失。"""
    backend = build_backend(make_config(tmp_path, backend="langfuse"))

    assert backend.name == "jsonl"
    assert "credentials missing" in (backend.degraded_reason or "")


def test_single_credential_degrades_to_local(tmp_path: Path) -> None:
    """只配一个 key 同样按未配置处理——半套凭据初始化必然失败在更深处。"""
    config = make_config(tmp_path, backend="langfuse", langfuse_public_key="pk-lf-demo")

    backend = build_backend(config)

    assert backend.name == "jsonl"
    assert "credentials missing" in (backend.degraded_reason or "")


def test_degraded_backend_still_records_to_files(tmp_path: Path) -> None:
    """降级之后仍要留下记录，「少记一点」不等于「什么都不记」。"""
    config = make_config(tmp_path, backend="langfuse")
    tracer = Tracer(config, build_backend(config))

    with tracer.trace("request", seed="degraded"), tracer.span("retrieve", kind="retriever"):
        pass
    tracer.flush()

    payloads = read_lines(tmp_path / "traces")
    assert [payload["name"] for payload in payloads] == ["retrieve", "request"]
    assert tracer.degraded_reason is not None
    assert "credentials missing" in tracer.degraded_reason


def test_metadata_keys_are_sanitized_for_the_remote_api() -> None:
    """键名转成只含字母数字的驼峰式；净化不出合法键名的一律丢弃。"""
    assert to_langfuse_metadata({"top1_score": 1, "hits": 2, "session_id": "s", "already": 3}) == {
        "top1Score": 1,
        "hits": 2,
        "sessionId": "s",
        "already": 3,
    }

    assert to_langfuse_metadata({"中文键": 1, "2start": 2, "": 3, "_": 4, "ok": 5}) == {"ok": 5}


def test_remote_backend_dispatches_without_content(tmp_path: Path) -> None:
    """走远端时：类型映射正确、层级靠上下文维持、载荷里没有正文。"""
    config = make_config(
        tmp_path,
        backend="langfuse",
        langfuse_public_key="pk-lf-demo",
        langfuse_secret_key="sk-lf-demo",
    )
    client = FakeLangfuseClient()
    backend = LangfuseBackend(config, client=client)
    tracer = Tracer(config, backend)

    with (
        tracer.trace("request", seed="cloud", session_id="sess-1"),
        tracer.span("retrieve", kind="retriever", top1_score=1, 中文键=3),
        tracer.span("generate", kind="generation") as generate,
    ):
        tracer.set_usage(model="qwen3:1.7b", input_tokens=10, output_tokens=5)
        generate.content = content_payload(LONG_TEXT, capture=False, max_chars=2000)
    tracer.flush()

    assert client.flush_count == 1
    assert [kwargs.get("as_type") for kwargs in client.created] == [
        "span",
        "retriever",
        "generation",
    ]
    assert all(context.entered and context.exited for context in client.contexts)
    assert backend.reported == 3

    assert client.created[0]["trace_context"] == {"trace_id": new_trace_id("cloud")}
    assert all("trace_context" not in kwargs for kwargs in client.created[1:])
    assert client.created[0]["metadata"] == {"sessionId": "sess-1"}

    assert "input" not in client.dispatched_keys()
    assert "output" not in client.dispatched_keys()
    assert not any(LONG_TEXT[:20] in str(kwargs) for kwargs in client.created)
    assert not any(
        LONG_TEXT[:20] in str(item)
        for observation in client.observations
        for item in observation.updates
    )

    generation = client.observations[2].merged()
    assert generation["model"] == "qwen3:1.7b"
    assert generation["usage_details"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    assert generation["metadata"]["durationMs"] >= 0


def test_remote_backend_marks_errors(tmp_path: Path) -> None:
    """失败 span 上报为错误级别，摘要取异常消息。"""
    config = make_config(
        tmp_path,
        backend="langfuse",
        langfuse_public_key="pk-lf-demo",
        langfuse_secret_key="sk-lf-demo",
    )
    client = FakeLangfuseClient()
    tracer = Tracer(config, LangfuseBackend(config, client=client))

    with (
        pytest.raises(RuntimeError, match="upstream"),
        tracer.trace("request"),
        tracer.span("retrieve", kind="retriever"),
    ):
        raise RuntimeError("upstream 503")

    assert [kwargs["name"] for kwargs in client.created] == ["request", "retrieve"]
    for observation in client.observations:
        assert observation.merged()["level"] == "ERROR"
        assert observation.merged()["status_message"] == "upstream 503"


async def test_remote_backend_releases_client(tmp_path: Path) -> None:
    """``aclose`` 刷新并关闭远端客户端，可直接注册进依赖容器。"""
    config = make_config(
        tmp_path,
        backend="langfuse",
        langfuse_public_key="pk-lf-demo",
        langfuse_secret_key="sk-lf-demo",
    )
    client = FakeLangfuseClient()
    tracer = Tracer(config, LangfuseBackend(config, client=client))

    await tracer.aclose()

    assert client.flush_count == 1
    assert client.shutdown_count == 1
