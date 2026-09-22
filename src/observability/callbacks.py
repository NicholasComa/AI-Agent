"""工作流节点埋点：把 LangGraph 的节点执行转换成 ``chain`` span。

为什么不用上下文管理器
----------------------

图节点的进出不是一对同栈的 ``with``：回调由 LangGraph 的内部 runner 发起，
``on_chain_start`` 与 ``on_chain_end`` 之间隔着调度，无法用 ``with`` 包裹。因此
这里手工维护「进入即建记录、结束即收尾」，并以 ``run_id`` 作为唯一键。

真实回调结构
------------

一次 7 节点运行会产生 **9 次** ``on_chain_start``，而不是 7 次。实测结构是：

.. code-block:: text

    root            run=…88aa  parent=-      node=None       入参 1 键
    ├─ classify     run=…88ae  parent=…88aa  node=classify   入参 1 键 ← 包装层
    │  └─ classify  run=…88af  parent=…88ae  node=classify   入参 6 键 ← 真实节点
    ├─ functional_points …                  node=functional_points  入参 6 键
    ├─ rag_retrieve  …                      node=rag_retrieve       入参 7 键
    ├─ risk          …                      node=risk               入参 9 键
    ├─ test_points   …                      node=test_points        入参 10 键
    └─ report        …                      node=report             入参 11 键

两点结论：

1. **按名字计数会把节点数翻倍。** ``langgraph_step`` 也区分不了这两层（两次
   都是 ``step=1``），判据必须是**结构性的**：包装层的 ``parent_run_id`` 指向
   的恰好是一个**同名** span。于是规则是「同名嵌套只保留最内层」。
2. **匿名根要丢弃。** 最外层 ``node=None``，与业务无关，不进 trace。

于是 span 数 = 节点数 = 7，且每个节点的父节点都落在图根上——正是「一条工作流
trace 里 chain span 数与图节点数一致」这个验收口径。

上下文的边界
------------

``ContextVar`` 不跨线程传播，而 LangGraph 的回调可能来自内部 runner 线程。因
此**父子关系不能依赖 ``Tracer`` 的上下文变量**，必须由 ``run_id`` 与
``parent_run_id`` 显式推导；只有「补根 span」这一步才读上下文（请求级 trace
已由路由在进入时建立，本方法通常直接命中）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from .models import SpanRecord, new_span_id, single_line, utc_now
from .tracer import Tracer

logger = logging.getLogger(__name__)

NODE_NAME_KEY = "langgraph_node"
"""节点名所在的元数据键。"""

NODE_PATH_KEY = "langgraph_path"
"""节点路径元数据键。

与 :data:`NODE_NAME_KEY` 一起做**双重判据**：匿名根两项都缺失，真实节点两项
都有值。只看 ``langgraph_node`` 会漏掉「键存在但值为 ``None``」的匿名回调。
"""

ROUTE_NODES = ("classify",)
"""带条件边的节点：这些节点的输出决定后续走哪条路径。"""

ROUTE_TARGETS = ("clarify", "functional_points")
"""条件边的可选去向，与 :func:`graph.nodes.route_after_classify` 的返回值一致。"""


class ObservabilityCallbackHandler(BaseCallbackHandler):
    """把节点执行记成 ``chain`` span 的回调处理器。

    一个 handler 实例对应一条 trace。请求级 ``start_trace`` 先建立根 span 时，
    节点的 ``chain`` span 会挂在那个根下面；没有活动 trace 时自行补一条根。

    Attributes:
        root_name: 没有活动 trace 时补建根 span 使用的名称。
    """

    raise_error = False
    """回调内部异常不外抛：埋点失败不得中断图执行。"""

    def __init__(self, tracer: Tracer, *, root_name: str = "workflow") -> None:
        """初始化。

        Args:
            tracer: 追踪门面。
            root_name: 补建根 span 时使用的 trace 名。
        """
        super().__init__()
        self._tracer = tracer
        self._root_name = root_name
        self._open: dict[str, SpanRecord] = {}
        self._names: dict[str, str] = {}
        self._parents: dict[str, str | None] = {}
        self._order: list[str] = []
        self._trace_id: str | None = None
        self._root_span_id: str | None = None
        self._pending: dict[str, SpanRecord] = {}
        self._wrappers: dict[str, int] = {}

    @property
    def open_count(self) -> int:
        """仍未收尾的节点数。"""
        return len(self._open)

    def span_names(self) -> list[str]:
        """按进入顺序返回真实节点名，便于断言节点序列。"""
        return list(self._order)

    # ------------------------------------------------------------------
    # 回调
    # ------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """节点开始执行：建一条 ``chain`` span。

        同名嵌套的包装层会在 :meth:`_promote` 里被折叠掉——它先建了记录，等
        内层同名节点出现时再把父节点改指到包装层的父节点，并丢弃包装层记录。
        """
        try:
            name = _node_name(metadata)
            if name is None:
                # 匿名根（pregel 整体）不是业务节点，不进 trace。
                return
            key = _key(run_id)
            if key in self._open:
                logger.debug("observability.chain_start_duplicate node=%s", name)
                return
            parent_key = _key(parent_run_id) if parent_run_id is not None else None
            record = self._build(name, inputs)
            self._open[key] = record
            self._names[key] = name
            self._parents[key] = parent_key
            self._promote(key, name, parent_key)
            self._order.append(name)
        except Exception as exc:  # noqa: BLE001 —— 埋点失败不得影响图执行
            logger.warning("observability.chain_start_failed err=%s", exc)

    def on_chain_end(
        self,
        outputs: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """节点执行结束：补路线与重试信息，收尾并投递。

        同名嵌套时**两条结束事件各有各的信息量**：内层（真实节点）返回该节点
        的直接结果（``classify`` 这里是选中的分支名），外层（包装层）返回写回
        state 的字典——``needs_clarify`` 只在外层出现。

        因此路由节点的内层结束**先暂存不投递**，等外层结束把 ``route_taken``
        补齐后再投递一次；否则要么丢掉路线信息，要么同一条记录写两遍、在
        JSONL 里变成两行重复 span。

        暂存的条件必须严格：**仅当确实还存在一个同名的未结束包装层时**才延后。
        早期版本只按「路由节点 + 还没拿到路线」判定，结果在直接驱动回调（没有
        包装层）的场景下会把记录一直压在暂存区里——既不出现在记录中，也不报
        错，看起来像「回调没生效」。这类静默丢失比抛异常更难排查。
        """
        try:
            key = _key(run_id)
            record = self._open.pop(key, None)
            self._names.pop(key, None)
            parent_key = self._parents.pop(key, None)
            if record is not None:
                record.attributes.update(_retry_attributes(outputs))
                route = _route_from_outputs(outputs)
                if route:
                    record.attributes["route_taken"] = route
                if (
                    record.name in ROUTE_NODES
                    and "route_taken" not in record.attributes
                    and self._has_same_name_wrapper(parent_key, record.name)
                ):
                    # 外层包装层还在执行，等它的结束事件补齐路线。
                    self._pending[record.name] = record
                    return
                if record.name in ROUTE_NODES and "route_taken" not in record.attributes:
                    # 没有外层可等，直接投递并把路线记成 unknown，而不是丢弃。
                    record.attributes["route_taken"] = "unknown"
                _emit(self._tracer, record)
                return
            # 没有对应的进入记录：本条属于已被折叠的包装层，把路线补给暂存的
            # 同名记录后投递。
            self._flush_pending(outputs)
        except Exception as exc:  # noqa: BLE001 —— 埋点失败不得影响图执行
            logger.warning("observability.chain_end_failed err=%s", exc)

    def _has_same_name_wrapper(self, parent_key: str | None, name: str) -> bool:  # noqa: ARG002
        """判断是否还存在可提供路线信息的同名包装层。

        判据落在 :attr:`_wrappers` 而不是 ``_names``：包装层在 :meth:`_promote`
        里已从 ``_names`` 移除（否则会被当成独立节点计数），但它的结束事件还没
        到，正是 ``route_taken`` 的来源。
        """
        return self._wrappers.get(name, 0) > 0

    def _flush_pending(self, outputs: Any) -> None:
        """给暂存的路由记录补上 ``route_taken`` 并投递。"""
        if not self._pending:
            return
        route = _route_from_outputs(outputs)
        name, record = self._pending.popitem()
        remaining = self._wrappers.get(name, 0) - 1
        if remaining > 0:
            self._wrappers[name] = remaining
        else:
            self._wrappers.pop(name, None)
        if route:
            record.attributes["route_taken"] = route
        elif name in ROUTE_NODES:
            # 外层没给出路线信息时仍要投递，避免记录丢失。
            record.attributes.setdefault("route_taken", "unknown")
        _emit(self._tracer, record)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """节点抛异常：标错误并投递；异常由图自身继续处理。"""
        try:
            key = _key(run_id)
            record = self._open.pop(key, None)
            self._names.pop(key, None)
            self._parents.pop(key, None)
            if record is None:
                return
            record.status = "error"
            record.error_type = type(error).__name__
            record.error_message = single_line(str(error)) or None
            record.attributes["error"] = type(error).__name__
            _emit(self._tracer, record)
        except Exception as exc:  # noqa: BLE001 —— 埋点失败不得影响图执行
            logger.warning("observability.chain_error_failed err=%s", exc)

    def close(self) -> None:
        """收尾所有仍在执行的节点。

        图被提前中断（澄清挂起、客户端断开）时会留下未收尾的节点，这里把它们
        标成 ``unfinished`` 后投递，而不是让记录悬空。暂存中的路由记录也一并
        投递，否则会被静默丢掉——挂起恰好发生在 ``clarify`` 路径上，那正是最
        需要看到路线的时候。
        """
        for key in list(self._open):
            record = self._open.pop(key)
            self._names.pop(key, None)
            self._parents.pop(key, None)
            try:
                record.status = "error"
                record.unfinished = True
                record.error_type = record.error_type or "UnfinishedNode"
                _emit(self._tracer, record)
            except Exception as exc:  # noqa: BLE001 —— 收尾失败只记日志
                logger.warning("observability.chain_close_failed err=%s", exc)
        while self._pending:
            _name, pending = self._pending.popitem()
            pending.attributes.setdefault("route_taken", "unknown")
            try:
                _emit(self._tracer, pending)
            except Exception as exc:  # noqa: BLE001 —— 收尾失败只记日志
                logger.warning("observability.chain_close_failed err=%s", exc)
        self._wrappers.clear()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build(self, name: str, inputs: Any) -> SpanRecord:
        """建一条 ``chain`` 记录，把 trace 标识与根 span 固定在实例上。

        **不能每次去读上下文变量**：``ContextVar`` 不跨线程传播，而回调可能由
        LangGraph 的内部 runner 线程发起。逐次读取会让每个节点各自补出一条新
        的根 span，产出的不是一棵树而是七棵——这正是「父子完整」要避免的。
        因此在第一次建记录时确定 trace 与根，此后一致复用。
        """
        tracer = self._tracer
        if self._trace_id is None:
            self._trace_id = tracer.current_trace_id()
            if self._trace_id is None:
                # 脚本直跑图时没有请求级 trace：补一条根，节点挂在它下面。
                self._trace_id = tracer.start_trace(self._root_name)
                root = tracer.current_span()
                self._root_span_id = root.span_id if root is not None else None
            else:
                root = tracer.current_span()
                self._root_span_id = root.span_id if root is not None else None
        return SpanRecord(
            trace_id=self._trace_id,
            span_id=new_span_id(),
            parent_id=None,
            name=name,
            kind="chain",
            started_at=utc_now(),
            request_id=None,
            attributes=_input_attributes(inputs),
        )

    def _promote(self, key: str, name: str, parent_key: str | None) -> None:
        """折叠同名嵌套的包装层。

        若直接父节点是一个**同名** span，说明当前这条才是真实节点、父节点只是
        调度器加的包装层：把当前记录的父节点改指到包装层的父节点（通常是图
        根），并丢弃包装层记录。

        这样层级与 span 数同时正确——既不多出一层同名壳，也不会让真实节点认
        一个已被丢弃的记录当父节点。

        被折叠的包装层标识记进 :attr:`_wrappers`：它的 ``on_chain_end`` 稍后
        仍会到达，而那条事件携带的 state 字典正是 ``route_taken`` 的来源。
        """
        while parent_key is not None and self._names.get(parent_key) == name:
            self._open.pop(parent_key, None)
            self._names.pop(parent_key, None)
            self._wrappers[name] = self._wrappers.get(name, 0) + 1
            parent_key = self._parents.pop(parent_key, None)
            # 继续上溯：理论上包装层只有一层，循环写法对多层嵌套同样成立。
        self._parents[key] = parent_key
        parent_record = self._open.get(parent_key) if parent_key else None
        if parent_record is not None:
            self._open[key].parent_id = parent_record.span_id
        else:
            # 父节点不是被追踪的节点（匿名根、或已被折叠掉的包装层）：挂到本
            # trace 的根 span 上，保证树的连通性。
            self._open[key].parent_id = self._root_span_id


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------


def _key(run_id: Any) -> str:
    """把 ``run_id`` 归一化成字典键。"""
    return str(run_id)


def _node_name(metadata: dict[str, Any] | None) -> str | None:
    """取节点名；匿名根返回 ``None``。

    判据落在元数据而不是 ``serialized``：实测节点的 ``serialized`` 里没有
    ``name`` 键（取到 ``None``），而匿名根两项元数据都缺失。
    """
    meta = metadata or {}
    node = meta.get(NODE_NAME_KEY)
    path = meta.get(NODE_PATH_KEY)
    if not node or not path:
        return None
    return str(node)


def _elapsed_ms(started_at: str, ended_at: str) -> float:
    """按两条 ISO8601 时间戳算耗时（毫秒）。"""
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
    except ValueError:  # pragma: no cover —— 时间戳由本模块生成
        return 0.0
    return round(max((end - start).total_seconds() * 1000, 0.0), 3)


def _emit(tracer: Tracer, record: SpanRecord) -> None:
    """补结束信息并投递；采样未命中时不投递。

    直接操作门面内部表：回调的进出不在同一栈帧，拿不到 ``with`` 的出口，
    收尾只能手工做。语义与 ``Tracer._close_span`` 一致，只是不碰上下文变量。
    """
    record.ended_at = utc_now()
    record.duration_ms = _elapsed_ms(record.started_at, record.ended_at)
    tracer._open.pop(record.span_id, None)  # noqa: SLF001 —— 同包内部协作
    if tracer._is_sampled(record.trace_id):  # noqa: SLF001 —— 同包内部协作
        tracer._emit_end(record)  # noqa: SLF001 —— 同包内部协作


def _input_attributes(inputs: Any) -> dict[str, Any]:
    """记录节点入参的**键集合**，不记值。

    入参里带需求原文与 RAG 上下文，属于敏感内容；这里只留字段名，让「这一步
    收到了什么」在结构上可见而不泄露正文。
    """
    if not isinstance(inputs, dict):
        return {}
    return {"input_keys": sorted(str(key) for key in inputs)[:32]}


def _route_from_outputs(outputs: Any) -> str | None:
    """从条件边判定节点的输出里提取 ``route_taken``。

    ``route_taken`` 不是 :class:`graph.state.WorkflowState` 的字段，只能从判定
    节点的输出反推。两种形态都要认：

    - 内层真实节点的返回是**分支名**字符串（``"functional_points"``）；
    - 外层包装层返回的是写回 state 的字典，含 ``needs_clarify``。
    """
    if isinstance(outputs, str) and outputs in ROUTE_TARGETS:
        return outputs
    if isinstance(outputs, dict):
        needs = outputs.get("needs_clarify")
        if needs is not None:
            return "clarify" if needs else "functional_points"
    return None


def _retry_attributes(outputs: Any) -> dict[str, Any]:
    """从节点输出的 ``errors`` 里提取最大重试次数。

    ``retry_count`` 同样不是 state 字段：重试次数由
    :func:`graph.nodes._call_chat_with_retry` 记在 ``errors`` 条目的 ``attempts``
    键上，取最大值即可回答「这条链路重试过几次」。
    """
    if not isinstance(outputs, dict):
        return {}
    errors = outputs.get("errors")
    if not isinstance(errors, list):
        return {}
    attempts = [
        item["attempts"]
        for item in errors
        if isinstance(item, dict) and isinstance(item.get("attempts"), int)
    ]
    if not attempts:
        return {}
    return {"retry_count": max(attempts)}


__all__ = ["NODE_NAME_KEY", "NODE_PATH_KEY", "ROUTE_NODES", "ObservabilityCallbackHandler"]
