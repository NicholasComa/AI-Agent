"""追踪门面：调用方只依赖这一个对象。

门面负责三件事：维护 span 的父子关系与上下文变量、把记录投递给后端、以及在
异常与提前退出时保证记录不丢。后端差异（本地落盘 / 远端上报 / 内存兜底）被
完全藏在 :class:`backends.TraceBackend` 之后，调用方不需要知道当前用的是哪个。

采样采用「按 trace 标识做确定性哈希」，而不是每次抛硬币。用随机数的话，
同一 trace 里的不同 span 会各自决定是否记录，产出的要么是残缺的树、要么是
重复的树；而按 trace 判定既保证整棵树同进同出，又不需要额外保存「这个 trace
是否被采样」的状态（长时间运行的服务不需要为此维护一张会持续增长的集合）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from . import context
from .backends import TraceBackend, build_backend
from .config import ObservabilityConfig
from .models import (
    DEFAULT_KIND,
    SPAN_KINDS,
    SpanRecord,
    make_usage,
    new_span_id,
    new_trace_id,
    sanitize_attributes,
    single_line,
    utc_now,
)
from .pricing import estimate_cost_usd

logger = logging.getLogger(__name__)

TRACE_KIND = "trace"
"""根 span 的类型标识。"""

_SAMPLE_SPACE = 0xFFFFFFFF
"""采样哈希的取值范围（8 位十六进制）。"""


class Tracer:
    """追踪门面。

    构造不抛异常：后端不可用时由工厂换成兜底实现，因此可以安全地放进服务
    启动链路。
    """

    def __init__(self, config: ObservabilityConfig, backend: TraceBackend) -> None:
        """初始化。

        Args:
            config: 追踪层配置。
            backend: 已经构造好的后端。
        """
        self._config = config
        self._backend = backend
        self._open: dict[str, SpanRecord] = {}

    @property
    def backend(self) -> TraceBackend:
        """当前后端。"""
        return self._backend

    @property
    def backend_name(self) -> str:
        """后端标识。"""
        return self._backend.name

    @property
    def degraded_reason(self) -> str | None:
        """降级原因，可直接写进依赖探活明细。"""
        return self._backend.degraded_reason

    @property
    def config(self) -> ObservabilityConfig:
        """追踪层配置。"""
        return self._config

    # ------------------------------------------------------------------
    # trace 级
    # ------------------------------------------------------------------

    def start_trace(
        self,
        name: str,
        *,
        seed: str | None = None,
        request_id: str | None = None,
        **attributes: Any,
    ) -> str:
        """开启一条 trace 的根 span。

        根 span 需要由 :meth:`end_trace` 收尾；成对调用容易漏，业务代码建议
        直接用 :meth:`trace` 上下文管理器。

        Args:
            name: trace 名称。
            seed: 用于生成可复现的 trace 标识（同一 seed 得到同一标识）。
            request_id: 请求标识；缺省自动读取请求中间件注入的上下文。
            **attributes: 附加到根 span 的元数据，经清洗后写入。

        Returns:
            32 位十六进制 trace 标识。
        """
        trace_id = new_trace_id(seed)
        record = SpanRecord(
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_id=None,
            name=name,
            kind=TRACE_KIND,
            started_at=utc_now(),
            request_id=request_id if request_id is not None else context.current_request_id(),
            attributes=self._clean(attributes),
        )
        context.set_trace_id(trace_id)
        context.set_span_id(record.span_id)
        self._open[record.span_id] = record
        self._emit_begin(record)
        return trace_id

    def end_trace(self, *, status_code: int | None = None) -> bool:
        """收尾当前 trace 的根 span。

        按「当前 trace 里 kind 为 trace 的那条未结束记录」定位，因此无论调用
        时嵌套多深都能正确收尾，不需要调用方持有记录对象。

        Args:
            status_code: HTTP 状态码；大于等于 500 时把根 span 标成错误。

        Returns:
            是否找到并收尾了根 span。
        """
        trace_id = context.get_trace_id()
        root = next(
            (
                record
                for record in self._open.values()
                if record.kind == TRACE_KIND and record.trace_id == trace_id
            ),
            None,
        )
        if root is None:
            return False
        if status_code is not None:
            root.attributes["status_code"] = status_code
            if status_code >= 500:
                root.status = "error"
                root.error_type = root.error_type or "HTTPError"
        self._close_span(root)
        return True

    @contextmanager
    def trace(
        self,
        name: str,
        *,
        seed: str | None = None,
        request_id: str | None = None,
        **attributes: Any,
    ) -> Iterator[SpanRecord]:
        """开启并收尾一条 trace，产出根记录。

        退出时若体内抛异常，根 span 会被标成错误但异常照常向上抛；这样
        「请求失败了」与「请求成功但结果不对」在 trace 上是能分开的。
        """
        self.start_trace(name, seed=seed, request_id=request_id, **attributes)
        root = self.current_span()
        if root is None:  # pragma: no cover —— start_trace 必然设置当前 span
            msg = "trace root span is missing"
            raise RuntimeError(msg)
        try:
            yield root
        except BaseException as exc:
            self._mark(record=root, error=exc)
            self._close_span(root)
            raise
        else:
            self._close_span(root)

    def current_trace_id(self) -> str | None:
        """当前 trace 标识。"""
        return context.get_trace_id()

    def current_span(self) -> SpanRecord | None:
        """当前上下文中尚未结束的 span 记录。"""
        span_id = context.get_span_id()
        if span_id is None:
            return None
        return self._open.get(span_id)

    # ------------------------------------------------------------------
    # span 级
    # ------------------------------------------------------------------

    @contextmanager
    def span(self, name: str, kind: str = DEFAULT_KIND, **attributes: Any) -> Iterator[SpanRecord]:
        """开启一个 span，退出时投递。

        嵌套 ``with`` 会自动形成父子链：父节点取自当前上下文。**没有活动的
        trace 时**，本 span 直接作为自己那条 trace 的根（``parent_id`` 为
        ``None``、标识新生成），不再额外造一个根 span——那样只会多出一条永远
        收不了尾的记录。因此脚本可以只用 ``with tracer.span(...)`` 就开始记录。

        Args:
            name: span 名称。
            kind: 取值见 :data:`models.SPAN_KINDS`；非法值回退 ``span``。
            **attributes: 附加元数据，经清洗后写入。

        Yields:
            本次 span 的记录对象；可在体内直接写 ``record.attributes``。
        """
        record = self._open_span(name, kind, attributes)
        try:
            yield record
        except BaseException as exc:
            self._mark(record=record, error=exc)
            self._close_span(record)
            raise
        else:
            self._close_span(record)

    def mark_error(self, error_type: str, message: str | None = None) -> bool:
        """把当前 span 标成错误。

        用于「深层函数捕获了异常，但手里没有 span 句柄」的场景，调用方不必把
        记录对象沿调用链一路传下来。

        Args:
            error_type: 异常类名或自定义错误类别。
            message: 可读摘要，会压成单行并截断。

        Returns:
            是否成功标记；当前没有活动 span 时返回 ``False``。
        """
        record = self.current_span()
        if record is None:
            return False
        record.status = "error"
        record.error_type = error_type
        if message:
            record.error_message = single_line(message)
        return True

    def set_attributes(self, **attributes: Any) -> bool:
        """给当前 span 追加元数据。

        Returns:
            是否写入成功；当前没有活动 span 时返回 ``False``。
        """
        record = self.current_span()
        if record is None:
            return False
        record.attributes.update(self._clean(attributes))
        return True

    def set_usage(
        self,
        *,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> bool:
        """给当前 span 记录模型名与 token 用量。

        Returns:
            是否写入成功；当前没有活动 span 时返回 ``False``。
        """
        record = self.current_span()
        if record is None:
            return False
        if model:
            record.model = model
        record.usage = make_usage(input_tokens=input_tokens, output_tokens=output_tokens)
        record.cost_usd = estimate_cost_usd(record.model, record.usage)
        return True

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """收尾遗留 span 并刷新后端。

        正常路径下 ``with`` 语法保证每个 span 都会退出，所以这里还能看到未
        结束的 span，只可能来自漏写出口、提前 ``return`` 或进程即将退出。
        这类情况必须**可见**，因此收尾成错误状态并标 ``unfinished``，而不是
        静默丢弃。
        """
        self._sweep_unfinished()
        try:
            self._backend.flush()
        except Exception as exc:  # noqa: BLE001 —— 追踪失败不得影响业务
            logger.warning("observability.flush_failed err=%s", exc)

    async def aclose(self) -> None:
        """收尾并释放后端；任何失败只记日志，绝不外抛。

        可直接注册进依赖容器的释放钩子。关闭阶段抛出的异常会掩盖真正的业务
        错误，因此这里统一吞掉。
        """
        self.flush()
        try:
            await self._backend.aclose()
        except Exception as exc:  # noqa: BLE001 —— 关闭失败不影响其它资源释放
            logger.warning("observability.close_failed err=%s", exc)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _open_span(self, name: str, kind: str, attributes: dict[str, Any]) -> SpanRecord:
        """建记录、压入未结束表并通知后端进入。"""
        if kind not in SPAN_KINDS:
            logger.debug("observability.span_kind_invalid kind=%s fallback=%s", kind, DEFAULT_KIND)
            kind = DEFAULT_KIND
        trace_id = context.get_trace_id() or new_trace_id()
        record = SpanRecord(
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_id=context.get_span_id(),
            name=name,
            kind=kind,
            started_at=utc_now(),
            request_id=context.current_request_id(),
            attributes=self._clean(attributes),
        )
        context.set_trace_id(trace_id)
        context.set_span_id(record.span_id)
        self._open[record.span_id] = record
        self._emit_begin(record)
        return record

    def _close_span(self, record: SpanRecord) -> None:
        """补结束时间、计算耗时与成本、投递快照，并恢复上下文。"""
        ended_at = utc_now()
        record.ended_at = ended_at
        record.duration_ms = _elapsed_ms(record.started_at, ended_at)
        if record.cost_usd is None and record.usage:
            record.cost_usd = estimate_cost_usd(record.model, record.usage)
        self._open.pop(record.span_id, None)
        context.set_span_id(record.parent_id)
        if record.kind == TRACE_KIND:
            # trace 结束后必须把 trace 标识也清掉：留着它会让此后新建的 span
            # 继承一个已经收尾的 trace，产出「一条 trace 里有两个根」的错乱
            # 记录（该 span 的父标识为空，看起来又像根）。
            context.set_trace_id(None)
        if self._is_sampled(record.trace_id):
            self._emit_end(record)

    def _mark(self, *, record: SpanRecord, error: BaseException) -> None:
        """把异常信息写进记录。"""
        record.status = "error"
        record.error_type = type(error).__name__
        record.error_message = single_line(str(error)) or None

    def _sweep_unfinished(self) -> None:
        """把仍未结束的 span 收尾成错误状态并投递。"""
        pending = [record for record in self._open.values() if record.ended_at is None]
        for record in pending:
            record.status = "error"
            record.unfinished = True
            record.error_type = record.error_type or "UnfinishedSpan"
            logger.debug(
                "observability.unfinished_span name=%s kind=%s span_id=%s",
                record.name,
                record.kind,
                record.span_id,
            )
            self._close_span(record)

    def _emit_begin(self, record: SpanRecord) -> None:
        """通知后端进入 span；采样未命中或后端失败时安静跳过。

        **必须与 :meth:`_emit_end` 用同一个采样判定**：这两个回调是一对。若只
        在结束时判采样，未命中的 span 会「进得去、出不来」——本地后端只是白
        记一次进入计数，远端后端却会把上下文管理器永久留在栈上，既泄漏 OTel
        上下文，也让后续 span 的父节点错位。
        """
        if not self._is_sampled(record.trace_id):
            return
        try:
            self._backend.begin(record)
        except Exception as exc:  # noqa: BLE001 —— 追踪失败不得影响业务
            logger.warning("observability.backend_begin_failed err=%s", exc)

    def _emit_end(self, record: SpanRecord) -> None:
        """投递快照给后端；失败只记日志。"""
        try:
            self._backend.end(record.snapshot())
        except Exception as exc:  # noqa: BLE001 —— 追踪失败不得影响业务
            logger.warning("observability.backend_end_failed err=%s", exc)

    def _clean(self, attributes: dict[str, Any]) -> dict[str, Any]:
        """按配置清洗属性。"""
        return sanitize_attributes(attributes, max_label_chars=self._config.max_label_chars)

    def _is_sampled(self, trace_id: str) -> bool:
        """按 trace 标识确定性地判断是否记录。

        全量采样时直接返回真，不做任何哈希运算，也不引入随机数——测试与离线
        评测需要可复现的结果。部分采样时用标识前 8 位作为均匀分布的伪随机源，
        保证同一 trace 的所有 span 得到同一结论。
        """
        if not self._config.enabled:
            return False
        if self._config.sample_rate >= 1.0:
            return True
        if self._config.sample_rate <= 0.0:
            return False
        return int(trace_id[:8], 16) / _SAMPLE_SPACE < self._config.sample_rate


def _elapsed_ms(started_at: str, ended_at: str) -> float:
    """按两条 ISO8601 时间戳算耗时（毫秒）。

    用记录里的时间戳相减，而不是另存一个只在进程内有意义的单调时钟起点：
    记录的每个字段都应能独立解读，混入进程内状态会让 JSONL 的字段含义取决于
    读取时机。
    """
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
    except ValueError:  # pragma: no cover —— 时间戳由本模块生成，格式恒定
        return 0.0
    return round(max((end - start).total_seconds() * 1000, 0.0), 3)


def build_tracer(
    config: ObservabilityConfig | None = None, *, client: object | None = None
) -> Tracer:
    """读配置、建后端、组装门面。**永不抛异常**。

    Args:
        config: 追踪层配置；缺省按环境变量解析。
        client: 远端 SDK 客户端替身，仅测试使用。

    Returns:
        可用的追踪器。后端全部不可用时仍会得到一个内存后端的门面。
    """
    resolved = config if config is not None else ObservabilityConfig.from_env()
    backend = build_backend(resolved, client=client)
    if backend.degraded_reason:
        logger.info(
            "observability.tracer_ready backend=%s degraded=%s",
            backend.name,
            backend.degraded_reason,
        )
    return Tracer(resolved, backend)


__all__ = ["TRACE_KIND", "Tracer", "build_tracer"]
