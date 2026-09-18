"""指标采集与 ``/metrics-summary`` 测试。

分两层验证，各自回答不同的问题：

* **契约层**（纯单元）：:func:`route_key` 的归一化规则与
  :class:`LatencyHistogram` 的分位估算。这两处是「指标可读性」的根基——
  键不收敛会让指标随运行时长无限膨胀，分位算法错了会让运维照着假数字扩容。
* **接口层**（走 HTTP）：请求量、状态码分布、模型调用、检索命中率、错误码归类
  是否与真实链路一致。这一层的价值在于：埋点分散在中间件、路由与守卫三处，
  只有从接口读回来才能确认它们统计的是同一件事。

关于断言口径：探针被刻意排除在指标之外（见 :data:`agent_service.metrics._PROBE_PATHS`），
因此每条用例在读取摘要前都要先打一次业务请求——否则读到的可能全是探针噪声，
测不出「业务请求有没有被统计」。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from service_fakes import (
    QUERY_HIT,
    QUERY_MISS,
    FakeCallResult,
    FakeMcpSession,
    FakeTool,
    Harness,
    build_deps,
)

from agent_service import (
    AgentServiceDeps,
    AgentServiceSettings,
    create_agent_service_app,
)
from agent_service.metrics import (
    LATENCY_BUCKETS_MS,
    ROUTE_UNKNOWN,
    LatencyHistogram,
    MetricsASGIMiddleware,
    MetricsRegistry,
    route_key,
)

RAG_PATH = "/v1/rag/answer"
"""RAG 问答路径。"""


async def _summary(harness: Harness) -> dict[str, object]:
    """读取一次指标摘要，顺带断言接口自身可用。"""
    response = await harness.client.get("/metrics-summary")
    assert response.status_code == 200, response.text
    return response.json()


def _harness_with_mcp(tmp_path: Path) -> tuple[AgentServiceDeps, FakeMcpSession]:
    """组装一个启用了假 MCP 的依赖容器。

    工具路由在 ``mcp`` 未就绪时直接 503，因此要验证工具调用的指标必须挂上替身。
    """
    session = FakeMcpSession(
        tools=[FakeTool("ping", "连通性检查")],
        results={"ping": FakeCallResult(structuredContent={"ok": True})},
    )
    return build_deps(tmp_path, mcp=session), session


@asynccontextmanager
async def _running(deps: AgentServiceDeps) -> AsyncIterator[httpx.AsyncClient]:
    """用给定容器起一个走 ASGI 的测试客户端。

    这些用例需要自定义容器（启用 MCP、注入慢模型），无法复用 ``harness`` 夹具，
    但客户端的组装方式必须与夹具完全一致——``trust_env=False`` 尤其不能漏：
    本机存在 ``HTTP_PROXY``，httpx 默认会把发往 ``127.0.0.1`` 的请求交给代理。
    """
    app = create_agent_service_app(deps=deps, version="0.1.0")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            trust_env=False,
        ) as client,
    ):
        yield client


# --------------------------------------------------------------------------
# 契约层：键归一化
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/tools/read_text_file/call", "/v1/tools/:name/call"),
        ("/v1/tools/ping/call", "/v1/tools/:name/call"),
        ("/v1/rag/answer", "/v1/rag/answer"),
        ("/health", "/health"),
    ],
)
def test_route_key_folds_tool_name(path: str, expected: str) -> None:
    """带路径参数的工具路由必须折叠成同一个键。

    否则每新增一个工具就会多出一个指标键，长时间运行的服务指标字典会无限增长。
    """
    assert route_key(path) == expected


def test_route_key_falls_back_to_unknown_for_empty_path() -> None:
    """空路径归到 ``unknown``，而不是产生一个空字符串键。"""
    assert route_key("") == ROUTE_UNKNOWN


# --------------------------------------------------------------------------
# 契约层：直方图与分位
# --------------------------------------------------------------------------


def test_histogram_keeps_bucket_count_aligned_with_bounds() -> None:
    """桶数比边界数多一个：最后一个是溢出桶，没有它极慢请求会被静默丢弃。"""
    histogram = LatencyHistogram()
    assert len(histogram.counts) == len(LATENCY_BUCKETS_MS) + 1


def test_histogram_percentile_returns_bucket_upper_bound() -> None:
    """p95 返回桶上界——注释里承诺「不会低估」，这里把承诺固化成断言。

    99 个样本落在 10ms 桶、1 个落在 10000ms 桶时，真实 p95 约 10ms，
    但按桶估算应返回 10.0（第一个包含第 95 个样本的桶的上界）。
    """
    histogram = LatencyHistogram()
    for _ in range(99):
        histogram.observe(5.0)
    histogram.observe(9999.0)
    assert histogram.samples == 100
    assert histogram.percentile_ms(0.95) == 10.0
    assert histogram.percentile_ms(1.0) == 10_000.0


def test_histogram_counts_samples_beyond_last_bucket() -> None:
    """超出最大边界的样本进溢出桶，不会被丢掉——丢样本等于低估延迟。"""
    histogram = LatencyHistogram()
    histogram.observe(60_000.0)
    assert histogram.counts[-1] == 1
    assert histogram.percentile_ms(0.95) == float(LATENCY_BUCKETS_MS[-1])


def test_histogram_without_samples_reports_zero() -> None:
    """没有样本时返回 0 而不是抛异常：服务刚启动就会有人抓指标。"""
    histogram = LatencyHistogram()
    assert histogram.average_ms == 0.0
    assert histogram.percentile_ms(0.95) == 0.0
    assert histogram.snapshot() == {"avg": 0.0, "samples": 0, "p95": 0.0}


# --------------------------------------------------------------------------
# 契约层：计数器语义
# --------------------------------------------------------------------------


def test_registry_flight_counter_never_goes_negative() -> None:
    """并发计数减到 0 就停住。

    异常路径下 ``request_finished`` 可能多调一次，如果允许负值，运维会看到
    ``in_flight=-1`` 这种无法解释的数字。
    """
    registry = MetricsRegistry()
    registry.request_finished()
    assert registry.summary()["in_flight"] == 0


def test_registry_hit_rate_is_zero_without_queries() -> None:
    """未发生检索时命中率按 0.0 报告，避免除零。"""
    assert MetricsRegistry().summary()["rag_hit_rate"] == 0.0


def test_registry_aggregates_requests_by_route_and_status() -> None:
    """同一路由的不同状态码分别计数，且能聚合回路由维度。"""
    registry = MetricsRegistry()
    registry.record_request("/v1/rag/answer", 200, 12.0)
    registry.record_request("/v1/rag/answer", 504, 800.0)
    summary = registry.summary()
    assert summary["requests_total"] == {"/v1/rag/answer|200": 1, "/v1/rag/answer|504": 1}
    assert summary["requests_by_route"] == {"/v1/rag/answer": 2}
    assert summary["requests_by_status"] == {"200": 1, "504": 1}


# --------------------------------------------------------------------------
# 接口层：摘要接口本身
# --------------------------------------------------------------------------


async def test_metrics_summary_exposes_full_contract(harness: Harness) -> None:
    """摘要字段齐全，且 ``extra=forbid`` 下拼装未漏参数。

    这条用例同时是「响应模型与 summary() 的键是否对齐」的守门人：少一个字段
    Pydantic 就会抛校验错，早于任何线上抓取暴露出来。
    """
    body = await _summary(harness)
    assert body["service"] == "agent-service"
    assert body["uptime_seconds"] >= 0
    assert body["request_id"]
    for field_name in (
        "requests_total",
        "requests_by_route",
        "requests_by_status",
        "in_flight",
        "latency_ms",
        "llm_calls",
        "rag_queries",
        "rag_hit_rate",
        "errors_by_code",
        "idempotency_replays",
    ):
        assert field_name in body, field_name
    assert 0.0 <= body["rag_hit_rate"] <= 1.0


async def test_probes_are_excluded_from_request_counts(harness: Harness) -> None:
    """探针不计入请求量。

    容器 Healthcheck 是秒级的，若计入总量，业务流量的变化会被彻底淹没。
    """
    for _ in range(3):
        assert (await harness.client.get("/health")).status_code == 200
    assert (await harness.client.get("/ready")).status_code == 200

    body = await _summary(harness)
    assert body["requests_by_route"] == {}
    assert body["requests_total"] == {}
    assert body["latency_ms"]["samples"] == 0


async def test_business_requests_are_counted_by_route(harness: Harness) -> None:
    """业务请求按归一化路由累计，且延迟样本数与之同步。"""
    for _ in range(2):
        response = await harness.post(RAG_PATH, {"question": QUERY_HIT, "min_score": 0.0})
        assert response.status_code == 200

    body = await _summary(harness)
    assert body["requests_by_route"] == {RAG_PATH: 2}
    assert body["requests_by_status"] == {"200": 2}
    assert body["latency_ms"]["samples"] == 2
    assert body["latency_ms"]["p95"] >= 0.0


# --------------------------------------------------------------------------
# 接口层：检索命中率与模型调用
# --------------------------------------------------------------------------


async def test_rag_hit_and_miss_are_reflected_in_hit_rate(harness: Harness) -> None:
    """命中率把「召回非空但分数不达标」算作未命中，与拒答口径一致。

    只按「召回非空」统计会让指标虚高：低分拒答明明没答出来，却被算成命中。
    同时验证低分拒答不消耗模型调用——拒答路径不该为一次注定失败的生成付费。
    """
    hit = await harness.post(RAG_PATH, {"question": QUERY_HIT, "min_score": 0.3})
    miss = await harness.post(RAG_PATH, {"question": QUERY_MISS, "min_score": 0.3})
    assert hit.status_code == 200
    assert miss.status_code == 200
    assert miss.json()["has_answer"] is False

    body = await _summary(harness)
    assert body["rag_queries"] == 2
    assert body["rag_hit_rate"] == 0.5
    assert body["llm_calls"] == 1
    assert harness.chat.call_count == 1


async def test_llm_calls_count_actual_invocations(harness: Harness) -> None:
    """模型调用按实际次数累加，而非按请求次数。

    一次问答可能触发多次调用（拒答重试），按请求计会让成本估算失真。
    """
    await harness.post(RAG_PATH, {"question": QUERY_HIT, "min_score": 0.0})
    first = await _summary(harness)
    await harness.post(RAG_PATH, {"question": QUERY_HIT, "min_score": 0.0})
    second = await _summary(harness)
    assert int(second["llm_calls"]) > int(first["llm_calls"])


# --------------------------------------------------------------------------
# 接口层：错误码与幂等
# --------------------------------------------------------------------------


async def test_error_codes_are_counted_by_unified_code(harness: Harness) -> None:
    """统一错误码是运维的聚合维度，必须与响应体里的 code 一致。

    404 与 422 都映射到 ``invalid_argument``，因此这里验证的是「同一码会累加」，
    而不是「每个状态码各占一个键」。
    """
    assert (await harness.client.get("/no-such-path")).status_code == 404
    assert (await harness.client.get("/no-such-path")).status_code == 404
    invalid = await harness.post(RAG_PATH, {"question": ""})
    assert invalid.status_code == 422

    body = await _summary(harness)
    assert body["errors_by_code"]["invalid_argument"] == 3


async def test_idempotency_replay_is_counted(tmp_path: Path) -> None:
    """幂等命中单独计数：它是「客户端重试率」最直接的观测。"""

    async def chat(messages: list[dict[str, str]]) -> str:
        return "{}"

    deps = build_deps(tmp_path, chat=chat)
    async with _running(deps) as client:
        payload = {"requirement_text": "开发一个电商网站，包含商品浏览功能。"}
        headers = {"Idempotency-Key": "metrics-replay-1"}
        first = await client.post(
            "/v1/workflow/requirement-analysis", json=payload, headers=headers
        )
        assert first.status_code == 200
        summary_before = (await client.get("/metrics-summary")).json()
        assert summary_before["idempotency_replays"] == 0

        second = await client.post(
            "/v1/workflow/requirement-analysis", json=payload, headers=headers
        )
        assert second.status_code == 200
        assert second.headers.get("idempotency-replayed") == "true"
        summary_after = (await client.get("/metrics-summary")).json()

    assert summary_after["idempotency_replays"] == 1


# --------------------------------------------------------------------------
# 接口层：工具路由的键归一化（真实路由，不只测函数）
# --------------------------------------------------------------------------


async def test_tool_calls_collapse_into_single_route_key(tmp_path: Path) -> None:
    """经真实路由打两次不同工具，指标里只应出现一个归一化键。

    这条用例是 :func:`route_key` 单元测试的补充：只有中间件真的把原始路径喂给
    了归一化函数，折叠才算生效。
    """
    deps, _ = _harness_with_mcp(tmp_path)
    async with _running(deps) as client:
        assert (await client.post("/v1/tools/ping/call", json={"arguments": {}})).status_code == 200
        assert (
            await client.post("/v1/tools/other/call", json={"arguments": {}})
        ).status_code == 200
        body = (await client.get("/metrics-summary")).json()

    assert body["requests_by_route"] == {"/v1/tools/:name/call": 2}


# --------------------------------------------------------------------------
# 接口层：/ready 顺带刷新的可核验数字
# --------------------------------------------------------------------------


async def test_ready_reports_points_count_for_qdrant(harness: Harness) -> None:
    """``/ready`` 的 qdrant 明细带上片段数。

    这是持久化测试的判据来源：重启前后片段数一致，才说明卷真的保住了。
    """
    body = (await harness.client.get("/ready")).json()
    qdrant = next(item for item in body["dependencies"] if item["name"] == "qdrant")
    assert "points_count=" in qdrant["detail"]
    assert qdrant["detail"].endswith("points_count=1")


async def test_ready_reports_tool_count_when_mcp_enabled(tmp_path: Path) -> None:
    """MCP 就绪时明细带上工具数；工具数为 0 说明子进程起来了却没注册成功。"""
    deps, _ = _harness_with_mcp(tmp_path)
    async with _running(deps) as client:
        body = (await client.get("/ready")).json()

    mcp = next(item for item in body["dependencies"] if item["name"] == "mcp")
    assert mcp["ready"] is True
    assert mcp["detail"].endswith("tools=1")


async def test_ready_detail_survives_repeated_calls(harness: Harness) -> None:
    """反复调用 ``/ready`` 不会让明细越拼越长。

    该接口每次都往明细里追加 ``points_count=``，若不去掉旧值，字符串会随调用
    次数无限增长——探针被高频调用时这会变成内存与日志噪音。
    """
    first = (await harness.client.get("/ready")).json()
    for _ in range(3):
        await harness.client.get("/ready")
    last = (await harness.client.get("/ready")).json()
    first_qdrant = next(item for item in first["dependencies"] if item["name"] == "qdrant")
    last_qdrant = next(item for item in last["dependencies"] if item["name"] == "qdrant")
    assert last_qdrant["detail"].count("points_count=") == 1
    assert last_qdrant["detail"] == first_qdrant["detail"]


# --------------------------------------------------------------------------
# 中间件：容器未就绪时不得影响请求
# --------------------------------------------------------------------------


async def test_metrics_middleware_tolerates_missing_container(tmp_path: Path) -> None:
    """容器尚未注册时中间件跳过统计，而不是把请求打成 500。

    请求可能早于 lifespan 完成（例如进程刚起来时的探活），此时 ``state.deps``
    还没挂上。中间件是观测设施，不能成为新的故障点。
    """
    deps = AgentServiceDeps.create(AgentServiceSettings(), version="0.1.0")
    app = create_agent_service_app(deps=deps, version="0.1.0")
    del app.state.deps

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        trust_env=False,
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dependency_unavailable"


def test_metrics_middleware_is_constructible_with_single_argument() -> None:
    """中间件必须能用 ``app.add_middleware`` 的单参数形式挂载。

    若构造函数要求额外参数，``add_middleware`` 就会报错——这条断言把这个约束
    固化下来，避免以后有人把它改成需要注入 registry。
    """

    async def app(scope: dict[str, object], receive: object, send: object) -> None:
        raise AssertionError("不应被调用")

    assert isinstance(MetricsASGIMiddleware(app), MetricsASGIMiddleware)
