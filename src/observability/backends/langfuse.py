"""Langfuse 后端：把记录上报到 Langfuse Cloud 或自托管实例。

为什么后端接口是 ``begin`` / ``end`` 两个回调
--------------------------------------------

远端 SDK 建在 OpenTelemetry 之上，**父子层级靠上下文管理器维持**，而不是靠
记录里事后补一个父标识。若只在 span 结束时投递一条记录，就只能改用显式的
``trace_context`` 指父，而官方明确提示：父 observation 若尚未被服务端收到，
子会被挂到 trace 根上。span 的结束顺序天生是「子先父后」，所以那条路不可靠。

因此本后端把 SDK 的上下文管理器**手动进出**并与 span 标识配对压栈：进入时
``__enter__``，结束时先 ``update`` 再 ``__exit__``。这样层级由 OTel 上下文自然
推导，与本地后端的「结束时追加一行」在语义上等价。

只上报元数据
------------

``begin`` / ``end`` **从不传** ``input`` / ``output``：SDK 只在显式传参时才会上
传内容，所以「不记正文」在这里是结构上成立的，而不是靠调用方自觉。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from contextlib import ExitStack
from typing import Any

from ..config import ObservabilityConfig
from ..models import SpanRecord
from .base import BackendUnavailableError

logger = logging.getLogger(__name__)

_AS_TYPE: dict[str, str] = {
    "trace": "span",
    "span": "span",
    "chain": "chain",
    "generation": "generation",
    "retriever": "retriever",
    "tool": "tool",
}
"""本地 kind → 远端 observation 类型。

远端原生支持 ``retriever`` / ``tool`` / ``chain``，因此不需要把真实语义塞进
元数据字段；只有根 span 例外——它没有对应的专用类型，统一用 ``span``。
"""

DEFAULT_AS_TYPE = "span"
"""类型映射缺失时的兜底值。"""

MAX_STACK_DEPTH = 256
"""未结束 span 的栈深上限。

正常一条 trace 只有十几个 span，触到上限说明存在未配对的进出（通常是异常
路径漏了出口）。此时继续压栈会连同 OTel 上下文一起泄漏，所以在超限时停止
记录并告警，而不是无限增长。
"""

_ALNUM_RUN = re.compile(r"[^A-Za-z0-9]+")


def to_langfuse_metadata(values: Mapping[str, Any]) -> dict[str, Any]:
    """把属性键净化成远端接受的形态。

    远端对 trace 级元数据的键名要求是「只含字母数字」，下划线会被拒绝或静默
    丢弃。这里把 ``snake_case`` 转成 ``camelCase``（``top1_score`` →
    ``top1Score``），无法净化出以字母开头的键一律丢弃。值的类型不变。

    本地 JSONL 保留原始键名，因此转换只发生在这一层——需要对照两个后端时，
    以本地文件的键名为准。

    Args:
        values: 已清洗的属数字典。

    Returns:
        键名可用于远端的新字典。
    """
    converted: dict[str, Any] = {}
    for key, value in values.items():
        parts = [part for part in _ALNUM_RUN.split(str(key)) if part]
        if not parts or not parts[0][:1].isalpha():
            logger.debug("observability.langfuse_metadata_key_dropped key=%s", key)
            continue
        head = parts[0]
        converted[head + "".join(part[:1].upper() + part[1:] for part in parts[1:])] = value
    return converted


def is_alnum_key(key: str) -> bool:
    """键名是否已满足「只含字母数字且以字母开头」。"""
    return bool(key) and key.isalnum() and key[:1].isalpha()


class LangfuseBackend:
    """上报到 Langfuse 的后端。

    远端 SDK 只在构造时导入：导入 SDK 会注册退出钩子并启动后台线程，放在包
    导入期会污染测试环境。
    """

    name = "langfuse"

    def __init__(self, config: ObservabilityConfig, *, client: Any = None) -> None:
        """初始化并取客户端。

        Args:
            config: 追踪层配置，读取端点与发布标识。
            client: 客户端替身；为 ``None`` 时取 SDK 单例（自动读环境变量）。

        Raises:
            BackendUnavailableError: SDK 导入失败或客户端构造抛异常。由工厂捕获
                并降级，调用方无需处理。
        """
        try:
            from langfuse import get_client, propagate_attributes
        except ImportError as exc:  # pragma: no cover —— 工厂已先探测过可导入性
            msg = f"langfuse sdk import failed: {exc}"
            raise BackendUnavailableError(msg) from exc

        self._config = config
        self._propagate_attributes = propagate_attributes
        self._stack: list[tuple[str, Any, ExitStack]] = []
        self._reported = 0
        try:
            self._client = client if client is not None else get_client()
        except Exception as exc:  # noqa: BLE001 —— 初始化失败统一转成降级信号
            msg = f"langfuse client init failed: {type(exc).__name__}: {exc}"
            raise BackendUnavailableError(msg) from exc

    @property
    def degraded_reason(self) -> str | None:
        """上报后端未降级，恒为 ``None``。"""
        return None

    @property
    def reported(self) -> int:
        """已上报的 span 条数，供冒烟脚本与测试核对。"""
        return self._reported

    def begin(self, record: SpanRecord) -> None:
        """进入一个 observation 并把上下文管理器压栈。"""
        if len(self._stack) >= MAX_STACK_DEPTH:
            logger.warning(
                "observability.langfuse_stack_full depth=%d name=%s", len(self._stack), record.name
            )
            return
        exit_stack = ExitStack()
        try:
            if record.kind == "trace":
                self._enter_trace(exit_stack, record)
            observation = self._enter_observation(exit_stack, record)
        except Exception as exc:  # noqa: BLE001 —— 追踪失败不得影响业务
            logger.warning("observability.langfuse_begin_failed name=%s err=%s", record.name, exc)
            exit_stack.close()
            return
        self._stack.append((record.span_id, observation, exit_stack))

    def end(self, record: SpanRecord) -> None:
        """更新并退出对应的 observation。"""
        entry = self._take(record.span_id)
        if entry is None:
            logger.debug("observability.langfuse_end_unpaired span_id=%s", record.span_id)
            return
        _, observation, exit_stack = entry
        try:
            observation.update(**self._update_kwargs(record))
        except Exception as exc:  # noqa: BLE001 —— 追踪失败不得影响业务
            logger.debug("observability.langfuse_update_failed name=%s err=%s", record.name, exc)
        finally:
            try:
                exit_stack.close()
            except Exception as exc:  # noqa: BLE001 —— 追踪失败不得影响业务
                logger.debug("observability.langfuse_exit_failed name=%s err=%s", record.name, exc)
        self._reported += 1

    def flush(self) -> None:
        """把缓冲内容推给远端。"""
        try:
            self._client.flush()
        except Exception as exc:  # noqa: BLE001 —— 追踪失败不得影响业务
            logger.warning("observability.langfuse_flush_failed err=%s", exc)

    async def aclose(self) -> None:
        """关闭客户端（内部含一次刷新）。"""
        try:
            self._client.shutdown()
        except Exception as exc:  # noqa: BLE001 —— 关闭失败不影响其它资源释放
            logger.warning("observability.langfuse_shutdown_failed err=%s", exc)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _enter_trace(self, exit_stack: ExitStack, record: SpanRecord) -> None:
        """为根 span 设定 trace 级属性。

        ``session_id`` 与 ``version`` 从根记录的属性里取（追踪层会把会话标识
        放进根 span），因此调用方不需要再学一套远端专用参数。
        """
        session_id = record.attributes.get("session_id")
        exit_stack.enter_context(
            self._propagate_attributes(
                trace_name=record.name or None,
                session_id=str(session_id) if session_id else None,
                version=self._config.langfuse_release or None,
            )
        )

    def _enter_observation(self, exit_stack: ExitStack, record: SpanRecord) -> Any:
        """创建 observation 并返回其对象。

        只有根 span 传 ``trace_context``，用它把本地生成的 trace 标识对齐到
        远端；子 span 的父节点由 OTel 上下文推导，不重复指定，避免与上下文
        冲突。``input`` / ``output`` 一律不传。
        """
        as_type = _AS_TYPE.get(record.kind, DEFAULT_AS_TYPE)
        kwargs: dict[str, Any] = {
            "as_type": as_type,
            "name": record.name or as_type,
        }
        if record.kind == "trace":
            kwargs["trace_context"] = {"trace_id": record.trace_id}
        metadata = to_langfuse_metadata(record.attributes)
        if metadata:
            kwargs["metadata"] = metadata
        return exit_stack.enter_context(self._client.start_as_current_observation(**kwargs))

    def _update_kwargs(self, record: SpanRecord) -> dict[str, Any]:
        """组装 ``update`` 的参数。

        只包含非空字段：远端把「显式传 None」当作清除该字段，把全部字段都传
        一遍会把已经设好的元数据覆盖掉。
        """
        metadata = to_langfuse_metadata(record.attributes)
        if record.duration_ms is not None:
            metadata["durationMs"] = record.duration_ms
        if record.unfinished:
            metadata["unfinished"] = True

        kwargs: dict[str, Any] = {}
        if metadata:
            kwargs["metadata"] = metadata
        if record.model:
            kwargs["model"] = record.model
        if record.usage:
            kwargs["usage_details"] = dict(record.usage)
        if record.cost_usd is not None:
            kwargs["cost_details"] = {"total": record.cost_usd}
        if record.status == "error":
            kwargs["level"] = "ERROR"
            kwargs["status_message"] = record.error_message or record.error_type or "error"
        return kwargs

    def _take(self, span_id: str) -> tuple[str, Any, ExitStack] | None:
        """从栈中取出指定 span 的条目。

        正常情况要退出的就是栈顶；若不是，说明退出顺序与进入顺序不一致，此时
        仍然按标识取出并告警——强行只处理栈顶会丢掉这条记录。
        """
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == span_id:
                if index != len(self._stack) - 1:
                    logger.warning(
                        "observability.langfuse_out_of_order span_id=%s depth=%d",
                        span_id,
                        index,
                    )
                return self._stack.pop(index)
        return None


__all__ = [
    "DEFAULT_AS_TYPE",
    "MAX_STACK_DEPTH",
    "LangfuseBackend",
    "is_alnum_key",
    "to_langfuse_metadata",
]
