"""进程内指标计数器：为 ``/metrics-summary`` 提供可读的运行期数字。

设计取舍
--------

**为什么未引 Prometheus**：本项目的可观测性需求是「服务跑起来之后，
运维能从一次 HTTP GET 里看出请求量、延迟、错误分布和依赖命中情况」。引入
``prometheus_client`` 会带来一个新的运行时依赖、一套独立的抓取协议与文本
暴露格式，而第 10 周要对接的是 Langfuse（trace 维度）而非 Prometheus（时间
序列维度）。因此这里用最小实现：纯内存字典加锁累加，零新依赖。

**为什么不在中间件之外重新计时**：:class:`middleware.AccessLogASGIMiddleware`
已经在最外层测了 ``duration_ms`` 并拿到了最终状态码，是天然的采集点。重复
计时会产生两套可能不一致的数字，因此这里只提供累加接口，由中间件调用。

**延迟分布用桶而非全量样本**：全量保存每个请求的耗时会随运行时长线性增长，
长时间运行的服务会因此泄漏内存。这里用固定边界的直方图（
:data:`LATENCY_BUCKETS_MS`），既能算 p95，占用又与请求量无关。

线程与协程安全
--------------

服务在单个事件循环里处理请求，理论上字典操作不会被打断；但指标累加可能
被非请求路径调用（例如测试里从线程驱动），因此统一用 :class:`threading.Lock`
保护。锁粒度只覆盖字典读写，不在持锁期间做任何 IO 或计算。
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

LATENCY_BUCKETS_MS: tuple[int, ...] = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10_000)
"""延迟直方图的桶上界（毫秒）。

取值覆盖本项目实测的两个量级：探针与校验类请求在 10ms 内，真实模型单次生成
在 1-80 秒。最后一个桶之上的样本计入 ``+Inf``，因此 p95 在极慢请求下会返回
一个「≥10000ms」的下界而不是失真的小值。
"""

ROUTE_UNKNOWN = "unknown"
"""无法归类到具体路由时的桶名。"""

_PROBE_PATHS = frozenset({"/health", "/ready", "/metrics-summary"})
"""不计入请求指标的探针路径。

容器的 Healthcheck 会以秒级频率打 ``/health``，若计入总量，业务请求数会被
稀释到看不出趋势。探针自身的可用性由编排系统判断，不需要在这里重复统计。
"""


def route_key(path: str) -> str:
    """把请求路径归一化成适合聚合的路由键。

    带路径参数的路由（``/v1/tools/{name}/call``）如果按原始路径统计，每个工具
    名都会单独占一个键，指标会随时间无限增长。这里把最后一段动态部分折叠成
    ``:name`` 占位，使同一条路由始终归到同一个键。

    Args:
        path: 原始请求路径，如 ``/v1/tools/read_text_file/call``。

    Returns:
        归一化路由键，如 ``/v1/tools/:name/call``；无法识别时返回原路径。
    """
    parts = [segment for segment in path.split("/") if segment] #按 " / " 切分路径得到列表，然后遍历，过滤掉空字符串，得到非空的路径段列表
    if len(parts) >= 3 and parts[0] == "v1" and parts[1] == "tools" and parts[-1] == "call":
        return "/v1/tools/:name/call"
    return path or ROUTE_UNKNOWN


@dataclass
class LatencyHistogram:
    """固定边界的延迟直方图。

    ``counts[i]`` 表示耗时落在 ``(LATENCY_BUCKETS_MS[i-1], LATENCY_BUCKETS_MS[i]]``
    区间的样本数，``counts[-1]`` 为超出最大边界的样本数。总和单独累计，
    便于算平均值而不必保留样本。
    """

    buckets: tuple[int, ...] = LATENCY_BUCKETS_MS
    counts: list[int] = field(default_factory=list)
    total_ms: float = 0.0
    samples: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            # 桶数比边界数多一个：最后一个是溢出桶。
            self.counts = [0] * (len(self.buckets) + 1)

    def observe(self, duration_ms: float) -> None:
        """记录一个样本。"""
        self.total_ms += duration_ms
        self.samples += 1
        for index, upper in enumerate(self.buckets):
            if duration_ms <= upper:
                self.counts[index] += 1
                return
        self.counts[-1] += 1

    @property
    def average_ms(self) -> float:
        """平均耗时；无样本时为 0.0。"""
        if not self.samples:
            return 0.0
        return self.total_ms / self.samples

    def percentile_ms(self, percentile: float) -> float:
        """按桶边界估算分位数。

        返回的是**桶上界**，而非线性插值结果。这样给出的 p95 是「95% 的请求
        不超过这个耗时」，偏保守但不会低估——对运维判断「要不要扩容」而言，
        低估比高估危险得多。

        Args:
            percentile: 0.0-1.0 之间的分位点。

        Returns:
            对应的桶上界毫秒数；无样本时为 0.0。
        """
        if not self.samples:
            return 0.0
        target = self.samples * max(0.0, min(percentile, 1.0))
        cumulative = 0
        for index, count in enumerate(self.counts):
            cumulative += count
            if cumulative >= target:
                if index < len(self.buckets):
                    return float(self.buckets[index])
                # 溢出桶：没有上界，返回最大边界作为下界估计。
                return float(self.buckets[-1])
        return float(self.buckets[-1])

    def snapshot(self, *, p95: bool = True) -> dict[str, float]:
        """导出平均值与分位数。"""
        payload = {"avg": round(self.average_ms, 3), "samples": self.samples}
        if p95:
            payload["p95"] = round(self.percentile_ms(0.95), 3)
        return payload


class MetricsRegistry:
    """服务运行期指标集合。

    所有写方法都以 ``record_`` 开头，读取统一走 :meth:`summary`，
    避免调用方直接触碰内部字典而绕过锁。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests_total: Counter[tuple[str, int]] = Counter()
        self._errors_by_code: Counter[str] = Counter()
        self._in_flight = 0
        self._latency = LatencyHistogram()
        self._llm_calls = 0
        self._rag_queries = 0
        self._rag_hits = 0
        self._idempotency_replays = 0

    # ----- 写入 -----

    def record_request(self, route: str, status_code: int, duration_ms: float) -> None:
        """记录一次已完成的请求。

        Args:
            route: 已归一化的路由键，通常来自 :func:`route_key`。
            status_code: 最终 HTTP 状态码。
            duration_ms: 端到端耗时（毫秒）。
        """
        with self._lock:
            self._requests_total[(route, status_code)] += 1
            self._latency.observe(duration_ms)

    def request_started(self) -> None:
        """进入一个请求，并发计数加一。"""
        with self._lock:
            self._in_flight += 1

    def request_finished(self) -> None:
        """一个请求结束，并发计数减一；不会低于 0。"""
        with self._lock:
            self._in_flight = max(self._in_flight - 1, 0)

    def record_error(self, code: str) -> None:
        """记录一次错误码。"""
        with self._lock:
            self._errors_by_code[code] += 1

    def record_llm_call(self, *, count: int = 1) -> None:
        """记录模型调用次数。

        一次 RAG 问答可能触发多次模型调用（拒答重试用更高 ``top_k`` 再问一次），
        因此这里按实际次数累加而不是按请求次数。
        """
        with self._lock:
            self._llm_calls += max(count, 0)

    def record_rag_query(self, *, hit: bool) -> None:
        """记录一次知识库检索及其是否命中。

        Args:
            hit: 是否召回到**可用**结果。按「召回非空且 Top1 分数达标」判定，
                与生成链路的拒答口径一致。
        """
        with self._lock:
            self._rag_queries += 1
            if hit:
                self._rag_hits += 1

    def record_idempotency_replay(self) -> None:
        """记录一次幂等缓存命中。"""
        with self._lock:
            self._idempotency_replays += 1

    # ----- 读取 -----

    @property
    def llm_calls(self) -> int:
        """累计模型调用次数。"""
        with self._lock:
            return self._llm_calls

    def summary(self) -> dict[str, Any]:
        """导出指标摘要，直接作为 ``/metrics-summary`` 的响应体。

        Returns:
            含 ``requests_total`` / ``in_flight`` / ``latency_ms`` / ``llm_calls``
            / ``rag_hit_rate`` / ``errors_by_code`` / ``idempotency_replays`` 的字典。
        """
        with self._lock:
            requests_total = {
                f"{route}|{code}": count
                for (route, code), count in sorted(self._requests_total.items())
            }
            by_route: dict[str, int] = {}
            by_status: dict[str, int] = {}
            for (route, code), count in self._requests_total.items():
                by_route[route] = by_route.get(route, 0) + count
                key = str(code)
                by_status[key] = by_status.get(key, 0) + count
            hit_rate = (self._rag_hits / self._rag_queries) if self._rag_queries else 0.0
            return {
                "requests_total": requests_total,
                "requests_by_route": dict(sorted(by_route.items())),
                "requests_by_status": dict(sorted(by_status.items())),
                "in_flight": self._in_flight,
                "latency_ms": self._latency.snapshot(),
                "llm_calls": self._llm_calls,
                "rag_queries": self._rag_queries,
                "rag_hit_rate": round(hit_rate, 4),
                "errors_by_code": dict(sorted(self._errors_by_code.items())),
                "idempotency_replays": self._idempotency_replays,
            }

    def reset(self) -> None:
        """清空全部计数。仅供测试使用，不在服务运行期调用。"""
        with self._lock:
            self._requests_total.clear()
            self._errors_by_code.clear()
            self._in_flight = 0
            self._latency = LatencyHistogram()
            self._llm_calls = 0
            self._rag_queries = 0
            self._rag_hits = 0
            self._idempotency_replays = 0


class MetricsASGIMiddleware:
    """请求计数与延迟采集中间件（纯 ASGI，不缓存响应体，与 SSE 兼容）。

    放在中间件栈的**最外层**，理由有二：

    1. 被内层拒绝的请求（认证 401、限流 429、请求体 413）也要计入请求量与状态
       码分布——它们恰恰是运维最关心的「为什么流量没进来」；
    2. 延迟要覆盖客户端的真实观感，包括中间件自身与响应发送时间。

    之所以不用 :class:`starlette.middleware.base.BaseHTTPMiddleware`：它会缓存
    响应体，与 SSE 流式端点不兼容（详见 :mod:`middleware`）。

    采集分两段：``http.response.start`` 给出状态码，请求结束时才能算出时长。
    ``in_flight`` 在进入时加、退出时减，因此采样瞬间的非零值表示确有请求在跑。

    依赖容器从 ASGI ``scope["app"].state.deps`` 读取，而不是构造参数——这样它
    可以直接用 ``app.add_middleware`` 挂载，与其它中间件保持一致的注册方式。
    容器尚未注册（请求早于 lifespan）时跳过统计，不影响请求本身。

    Args:
        app: 下游 ASGI 应用。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        registry = _registry_of(scope)
        path = scope.get("path", "")
        # 探针自身不计入：容器 Healthcheck 每秒都在打，会把业务流量稀释成噪声。
        if registry is None or path in _PROBE_PATHS:
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = 500  # 兜底：若下面是异常路径未发 response.start
        registry.request_started()

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            registry.request_finished()
            duration_ms = (time.perf_counter() - start) * 1000
            registry.record_request(route_key(path), status_code, duration_ms)


def _registry_of(scope: dict[str, Any]) -> MetricsRegistry | None:
    """从 ASGI scope 取出指标计数器；容器未就绪时返回 ``None``。"""
    application = scope.get("app")
    deps = getattr(getattr(application, "state", None), "deps", None)
    return getattr(deps, "metrics", None)

#明确公开接口，避免import * 污染
__all__ = [
    "LATENCY_BUCKETS_MS",
    "ROUTE_UNKNOWN",
    "LatencyHistogram",
    "MetricsASGIMiddleware",
    "MetricsRegistry",
    "route_key",
]
