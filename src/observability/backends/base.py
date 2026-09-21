"""追踪后端契约与兜底实现。

后端职责被刻意压到最小：**开始**与**结束**各一个回调，加收尾两个方法。
所有实现都必须遵守同一条纪律——**永不向业务链路抛异常**，失败只记日志。
追踪是旁路能力，它坏掉不该让请求返回 500。
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Protocol, runtime_checkable

from ..models import SpanRecord

logger = logging.getLogger(__name__)

DEFAULT_MAX_RECORDS = 1000
"""内存后端保留的记录条数上限。

内存后端在生产路径上会作为「落盘不可用」的兜底，若不加限制，一个长跑服务
会把它变成内存泄漏点。超出后丢弃最旧的记录并计数，让「丢了多少」可见。
"""


class BackendUnavailableError(RuntimeError):
    """后端构造失败（目录不可写、SDK 初始化异常等）。

    只由构造函数抛出，由后端工厂捕获并降级；运行期单个 span 写入失败不使用
    本异常，那种情况只记日志。
    """


@runtime_checkable
class TraceBackend(Protocol):
    """追踪后端契约。

    ``begin`` / ``end`` 是同步方法：本地后端是「打开-追加-关闭」的短写，远端
    SDK 的更新与刷新也是同步接口；只有 ``aclose`` 声明为异步，以便直接注册
    进依赖容器的释放钩子列表（其元素类型是「无参、返回 awaitable」）。

    Attributes:
        name: 后端标识，用于日志与探针展示。
        degraded_reason: 降级原因；未降级时为 ``None``。
    """

    name: str

    @property
    def degraded_reason(self) -> str | None:
        """降级原因，未降级时为 ``None``。"""
        ...

    def begin(self, record: SpanRecord) -> None:
        """进入一个 span。"""
        ...

    def end(self, record: SpanRecord) -> None:
        """结束一个 span；记录已由调用方深拷贝。"""
        ...

    def flush(self) -> None:
        """把缓冲区内容投递出去。"""
        ...

    async def aclose(self) -> None:
        """释放资源。"""
        ...


class InMemoryBackend:
    """纯内存后端：测试替身，同时也是「落盘不可用」时的最后兜底。

    同时承担两个角色是有意的：测试里需要观察「后端收到了什么」，生产路径在
    落盘目录不可写时也需要一个不抛异常的去处。两者的行为要求完全一致。
    """

    name = "memory"

    def __init__(
        self, *, reason: str | None = None, max_records: int = DEFAULT_MAX_RECORDS
    ) -> None:
        """初始化。

        Args:
            reason: 降级原因。用于区分「测试主动使用内存后端」与「落盘失败被
                动退到内存」——两者的排查方向完全不同。
            max_records: 保留的记录上限，超出后丢弃最旧的。
        """
        self._reason = reason
        self._max_records = max(max_records, 1)
        self.records: deque[SpanRecord] = deque(maxlen=self._max_records)
        self.begin_count = 0
        self.end_count = 0
        self.flush_count = 0
        self.dropped = 0
        self.closed = False

    @property
    def degraded_reason(self) -> str | None:
        """降级原因。"""
        return self._reason

    def begin(self, record: SpanRecord) -> None:
        """记录一次进入；不保存快照，避免同一 span 在列表中重复出现。"""
        self.begin_count += 1

    def end(self, record: SpanRecord) -> None:
        """收下已结束的记录快照；超出上限时丢弃最旧的一条。"""
        if len(self.records) == self._max_records:
            self.dropped += 1
        self.end_count += 1
        self.records.append(record)

    def flush(self) -> None:
        """无缓冲区，仅计数。"""
        self.flush_count += 1

    async def aclose(self) -> None:
        """标记为已关闭。"""
        self.closed = True

    def read_all(self) -> list[dict[str, Any]]:
        """按投递顺序导出载荷，接口与本地 JSONL 后端的读回方法一致。"""
        return [record.to_payload() for record in self.records]


__all__ = ["DEFAULT_MAX_RECORDS", "BackendUnavailableError", "InMemoryBackend", "TraceBackend"]
