"""``observability`` 测试的替身与工具函数。

独立成模块而不是塞进 ``conftest``：``tests/`` 不是包，跨测试文件复用只能靠
模块导入，而 ``conftest`` 这个名字已被顶层 ``tests/conftest.py`` 占用，同名会
冲突。约定与 ``tests/agent_service/service_fakes.py`` 一致。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from observability import ObservabilityConfig, Tracer
from observability.backends import JsonlBackend, build_backend
from observability.config import DEFAULT_LANGFUSE_BASE_URL, DEFAULT_TRACE_DIR
from observability.models import SpanRecord

LONG_TEXT = "这是内部手册的一段原文，绝不应该出现在追踪记录里。" * 40
"""用于验证「未开启内容捕获时原文不落盘」的长正文。"""

SOURCE_NAME = "jwipc_internal_manual.md"
"""用于验证来源文件名不落盘的文件名。"""


class RecordingBackend:
    """把进出回调收到的内容收进内存，避免每条用例都去读文件。"""

    name = "recording"

    def __init__(self, *, reason: str | None = None) -> None:
        """初始化。"""
        self._reason = reason
        self.begun: list[SpanRecord] = []
        self.records: list[SpanRecord] = []
        self.flush_count = 0
        self.closed = False

    @property
    def degraded_reason(self) -> str | None:
        """降级原因。"""
        return self._reason

    def begin(self, record: SpanRecord) -> None:
        """记录进入；存快照以便后续断言不受调用方继续改写影响。"""
        self.begun.append(record.snapshot())

    def end(self, record: SpanRecord) -> None:
        """记录结束。"""
        self.records.append(record)

    def flush(self) -> None:
        """计数。"""
        self.flush_count += 1

    async def aclose(self) -> None:
        """标记关闭。"""
        self.closed = True

    def by_name(self, name: str) -> SpanRecord:
        """按名称取唯一一条结束记录；缺失或多条时断言失败。"""
        matches = [record for record in self.records if record.name == name]
        assert len(matches) == 1, f"expected exactly one span named {name!r}, got {len(matches)}"
        return matches[0]

    def payload(self) -> str:
        """把所有已结束记录序列化成文本，用于整体检查是否出现原文。"""
        import json

        return "\n".join(
            json.dumps(record.to_payload(), ensure_ascii=False) for record in self.records
        )


class FakeObservation:
    """替身 observation：记录 ``update`` 收到的参数。"""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> FakeObservation:
        """记录一次更新并返回自身，模拟链式调用。"""
        self.updates.append(kwargs)
        return self

    def merged(self) -> dict[str, Any]:
        """把多次更新合并成一个字典，便于断言。"""
        merged: dict[str, Any] = {}
        for item in self.updates:
            merged.update(item)
        return merged


class _ObservationContext(AbstractContextManager[FakeObservation]):
    """替身上下文管理器：进入时产出 :class:`FakeObservation`。

    记录进出标志，用来验证后端**成对**调用了上下文管理器——配对的进出是远端
    父子层级能推导出来的前提，只进不出会让栈越积越深。
    """

    def __init__(self, observation: FakeObservation, kwargs: dict[str, Any]):
        self._observation = observation
        self._kwargs = kwargs
        self.entered = False
        self.exited = False

    def __enter__(self) -> FakeObservation:
        self.entered = True
        return self._observation

    def __exit__(self, *exc_info: object) -> None:
        self.exited = True
        return


class FakeLangfuseClient:
    """替身远端客户端：记录 observation 的创建参数与进出顺序。"""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.observations: list[FakeObservation] = []
        self.contexts: list[_ObservationContext] = []
        self.flush_count = 0
        self.shutdown_count = 0

    def start_as_current_observation(self, **kwargs: Any) -> _ObservationContext:
        """创建一条替身 observation 并记录调用参数。"""
        observation = FakeObservation()
        context = _ObservationContext(observation, kwargs)
        self.created.append(kwargs)
        self.observations.append(observation)
        self.contexts.append(context)
        return context

    def flush(self) -> None:
        """计数。"""
        self.flush_count += 1

    def shutdown(self) -> None:
        """计数。"""
        self.shutdown_count += 1

    def dispatched_keys(self) -> set[str]:
        """所有创建与更新调用出现过的参数名，用于断言绝不出现 ``input`` / ``output``。"""
        keys: set[str] = set()
        for kwargs in self.created:
            keys.update(kwargs)
        for observation in self.observations:
            for update in observation.updates:
                keys.update(update)
        return keys


def make_config(
    tmp_path: Path,
    *,
    capture_content: bool = False,
    sample_rate: float = 1.0,
    max_content_chars: int = 2000,
    max_label_chars: int = 64,
    backend: str = "local",
    enabled: bool = True,
    langfuse_public_key: str = "",
    langfuse_secret_key: str = "",
    langfuse_base_url: str = DEFAULT_LANGFUSE_BASE_URL,
) -> ObservabilityConfig:
    """构造指向临时目录的配置，避免任何用例写进仓库。"""
    return ObservabilityConfig(
        enabled=enabled,
        backend=backend,
        trace_dir=tmp_path / "traces",
        capture_content=capture_content,
        max_content_chars=max_content_chars,
        max_label_chars=max_label_chars,
        sample_rate=sample_rate,
        langfuse_public_key=langfuse_public_key,
        langfuse_secret_key=langfuse_secret_key,
        langfuse_base_url=langfuse_base_url,
    )


def make_tracer(
    tmp_path: Path,
    *,
    backend: RecordingBackend | None = None,
    **config_kwargs: Any,
) -> Tracer:
    """组装追踪器：默认走本地后端，也可注入记录型替身后端。"""
    config = make_config(tmp_path, **config_kwargs)
    if backend is not None:
        return Tracer(config, backend)
    return Tracer(config, build_backend(config))


def make_jsonl_backend(tmp_path: Path, **kwargs: Any) -> JsonlBackend:
    """构造指向临时目录的本地后端。"""
    directory = kwargs.pop("directory", tmp_path / "traces")
    return JsonlBackend(directory, **kwargs)


def read_lines(directory: Path) -> list[dict[str, Any]]:
    """读回目录下全部 JSONL 行；坏行跳过，口径与本地后端一致。"""
    import json

    payloads: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payloads.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return payloads


def trace_files(directory: Path) -> list[Path]:
    """目录下按天切片的追踪文件。"""
    return sorted(directory.glob("traces-*.jsonl"))


__all__ = [
    "DEFAULT_TRACE_DIR",
    "LONG_TEXT",
    "SOURCE_NAME",
    "FakeLangfuseClient",
    "FakeObservation",
    "RecordingBackend",
    "make_config",
    "make_jsonl_backend",
    "make_tracer",
    "read_lines",
    "trace_files",
]
