"""Day 9 并发限制测试 (asyncio.Semaphore + LlmClient.max_concurrency)。

覆盖 Roadmap 第 2 周阶段任务里明确列出的边界场景:
* **限流**:Semaphore 真的限制同时 in-flight 数量
* **错误释放**:失败后锁被正确释放(否则后续请求永远 hang)
* **配置校验**:``max_concurrency <= 0`` 抛 ``ValueError``
* **端到端**:并发 12 个 ``/chat`` 端点,至少 1 个走 semaphore 排队

策略:用真实 :class:`LlmClient` + ``httpx.MockTransport`` 注入慢 handler,
让 Semaphore 在真实代码路径里起作用。``MockTransport`` 是被 httpx 内部
调用的 —— 因此能精确反映 LlmClient.chat 的真实并发行为。
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app import create_app
from config import AppConfig
from llm_client import LlmClient, LlmResult, LlmServerError

# ---------------------------------------------------------------------------
# 慢响应 handler —— MockTransport 实际调它
# ---------------------------------------------------------------------------


class _SlowHandler:
    """一个 httpx handler,每次进入增加 current_count,记录 peak,然后 sleep。

    真实 LlmClient.chat 会先 acquire Semaphore,然后调 httpx 发起请求,
    httpx 内部用 transport 调到这个 handler。所以 peak_count 反映
    "LlmClient 同时持有 Semaphore 的数量",这正是我们要测的。
    """

    def __init__(self, sleep_ms: int = 50) -> None:
        self.sleep_ms = sleep_ms
        self._lock = asyncio.Lock()
        self.current_count = 0
        self.peak_count = 0
        self.call_count = 0
        self.should_raise = False

    def set_should_raise(self, value: bool) -> None:
        self.should_raise = value

    async def _on_enter(self) -> None:
        async with self._lock:
            self.call_count += 1
            self.current_count += 1
            self.peak_count = max(self.peak_count, self.current_count)

    async def _on_exit(self) -> None:
        async with self._lock:
            self.current_count -= 1

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await self._on_enter()
        try:
            if self.should_raise:
                return httpx.Response(500, json={"error": "simulated"})
            await asyncio.sleep(self.sleep_ms / 1000)
            body = {
                "choices": [{"message": {"content": "ok"}, "index": 0}],
                "model": "fake-model",
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            }
            return httpx.Response(200, json=body)
        finally:
            await self._on_exit()


def _make_llm(handler: _SlowHandler, max_concurrency: int = 3) -> LlmClient:
    """构造一个挂 MockTransport 的 LlmClient。"""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(timeout=2.0, transport=transport)
    return LlmClient(
        base_url="http://fake/v1",
        model="fake-model",
        api_key="test-key",
        client=client,
        max_concurrency=max_concurrency,
        # 重试关掉,便于断言精确次数
        max_retries=0,
    )


# ---------------------------------------------------------------------------
# 单元测试 - LlmClient 层面的 Semaphore
# ---------------------------------------------------------------------------


async def test_semaphore_limits_concurrent_requests() -> None:
    """10 个并发请求,峰值 in-flight 不得超过 ``max_concurrency=3``。"""
    handler = _SlowHandler(sleep_ms=50)
    llm = _make_llm(handler, max_concurrency=3)

    try:
        await asyncio.gather(
            *[llm.chat([{"role": "user", "content": f"q-{i}"}]) for i in range(10)]
        )
    finally:
        await llm.aclose()

    assert handler.call_count == 10
    assert handler.peak_count <= 3, f"expected peak <= 3, got {handler.peak_count}"
    # 强一点的断言:应该触达上限 3 附近(否则测试本身没意义)
    assert handler.peak_count == 3, (
        f"expected peak exactly 3, got {handler.peak_count} (可能 sleep 太长/任务数不够,测试无意义)"
    )


async def test_semaphore_releases_after_error() -> None:
    """500 错误后 Semaphore 必须被释放(否则后续请求永远 hang)。"""
    handler = _SlowHandler(sleep_ms=20)
    llm = _make_llm(handler, max_concurrency=2)

    try:
        # 第一个请求:会得到 500 → 抛 LlmServerError
        handler.set_should_raise(True)
        with pytest.raises(LlmServerError):
            await llm.chat([{"role": "user", "content": "fail"}])

        # 第二个请求:必须能拿锁。如果 Semaphore 没释放,会 hang 在
        # acquire 上,直到测试超时。我们设个 1s 超时保护。
        handler.set_should_raise(False)
        result = await asyncio.wait_for(
            llm.chat([{"role": "user", "content": "ok"}]),
            timeout=1.0,
        )
        assert result.text == "ok"
    finally:
        await llm.aclose()


def test_max_concurrency_zero_or_negative_rejected() -> None:
    """``max_concurrency <= 0`` 必须在 ``__init__`` 抛 ``ValueError``。"""
    with pytest.raises(ValueError, match="max_concurrency"):
        LlmClient(
            base_url="http://fake/v1",
            model="fake-model",
            api_key="test-key",
            max_concurrency=0,
        )
    with pytest.raises(ValueError, match="max_concurrency"):
        LlmClient(
            base_url="http://fake/v1",
            model="fake-model",
            api_key="test-key",
            max_concurrency=-1,
        )


# ---------------------------------------------------------------------------
# 端到端测试 - HTTP /chat 端点的并发行为
# ---------------------------------------------------------------------------


async def test_concurrent_chat_endpoint_respects_semaphore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """12 个并发 ``/chat`` POST,``max_concurrency=4``,至少 1 个走排队。"""
    handler = _SlowHandler(sleep_ms=50)
    llm = _make_llm(handler, max_concurrency=4)
    cfg = AppConfig(
        model_name="fake-model",
        api_base_url="http://fake/v1",
        timeout_seconds=2.0,
        enable_stream=True,
        log_format="plain",
        max_concurrency=4,  # 强制小上限
    )

    # 用 monkeypatch.setenv 而不是直接 os.environ[...],保证
    # 测试结束后环境被自动还原(否则会污染后续 test_config 等)
    monkeypatch.setenv("API_KEY", "test-key")

    fastapi_app = create_app(
        llm_factory=lambda: llm,
        config_loader=lambda: cfg,
    )
    asgi_app = fastapi_app

    durations: list[float] = []
    async with (
        fastapi_app.router.lifespan_context(fastapi_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app),
            base_url="http://test",
            timeout=10.0,
        ) as ac,
    ):

        async def _one() -> None:
            start = time.perf_counter()
            r = await ac.post(
                "/chat",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            durations.append(time.perf_counter() - start)
            assert r.status_code == 200

        try:
            await asyncio.gather(*[_one() for _ in range(12)])
        finally:
            await llm.aclose()

    # 关键断言
    assert handler.call_count == 12
    assert handler.peak_count <= 4, f"peak {handler.peak_count} > max_concurrency 4"
    # 至少有一个请求的端到端耗时 ≥ 100ms —— 12 个任务,4 并发,每 50ms:
    # 第一批 4 个 ≈ 50ms,第二批 4 个 ≈ 100ms,第三批 4 个 ≈ 150ms;
    # 如果完全并行则所有 ≈ 50ms。
    assert max(durations) >= 0.1, f"no serialization detected, all durations < 100ms: {durations}"


# ---------------------------------------------------------------------------
# 杂项:LlmResult 类型 re-export(防止 lint 工具报 unused import)
# ---------------------------------------------------------------------------


__all__ = ["LlmResult", "LlmClient"]
