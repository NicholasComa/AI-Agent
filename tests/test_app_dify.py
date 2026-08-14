"""Day 19 — ``POST /dify/run`` FastAPI 端点测试。

用 fake ``DifyWorkflowClient`` 替换真实客户端，不调用本地 Dify 服务。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app import create_app
from config import AppConfig


class _MinimalFakeLlm:
    """让 lifespan 能正常启动/关闭的最小 LLM 替身。"""

    async def aclose(self) -> None:
        pass

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def base_url(self) -> str:
        return "http://fake/v1"

    @property
    def timeout_seconds(self) -> float:
        return 1.0


class _FakeDifyClient:
    """测试替身：模拟 DifyWorkflowClient.run。"""

    def __init__(self, outputs: dict[str, Any], status: str = "succeeded") -> None:
        self.outputs = outputs
        self.status = status
        self.calls: list[str] = []

    async def run(
        self,
        query: str,
        *,
        user: str = "default",
        response_mode: str = "blocking",
    ) -> Any:
        self.calls.append(query)
        return type(
            "_Result",
            (),
            {
                "query": query,
                "outputs": self.outputs,
                "workflow_run_id": "wr-fake",
                "task_id": "task-fake",
                "status": self.status,
                "elapsed_time": 0.42,
                "total_tokens": 50,
                "raw": {},
            },
        )()


@pytest.fixture
def fake_config() -> AppConfig:
    return AppConfig(
        model_name="fake-model",
        api_base_url="http://fake/v1",
        api_key="test-key",
        timeout_seconds=1.0,
        enable_stream=False,
    )


@pytest.fixture
async def client(
    fake_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """注入 fake Dify 客户端的 FastAPI 测试客户端。

    outputs 用中性示例字段 ``result``，对应「工作流无关的通用透传」语义；
    实际调用哪个工作流由 ``.env`` 的 ``DIFY_API_KEY`` 决定。
    """
    fake_dify = _FakeDifyClient(
        outputs={
            "result": {"answer": "计算结果为 2"},
        }
    )

    def _fake_client(*args: Any, **kwargs: Any) -> _FakeDifyClient:  # noqa: ARG001
        return fake_dify

    monkeypatch.setattr("app.DifyWorkflowClient", _fake_client)

    fastapi_app = create_app(
        llm_factory=lambda: _MinimalFakeLlm(),
        config_loader=lambda: fake_config,
    )
    async with (
        fastapi_app.router.lifespan_context(fastapi_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fastapi_app),
            base_url="http://test",
        ) as ac,
    ):
        yield ac


async def test_dify_run_success(client: httpx.AsyncClient) -> None:
    """正常路径返回 200 和 DifyRunResponse。"""
    resp = await client.post("/dify/run", json={"query": "算一下 1+1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "算一下 1+1"
    assert body["outputs"]["result"]["answer"] == "计算结果为 2"
    assert body["status"] == "succeeded"
    assert body["elapsed_time"] == 0.42
    assert body["total_tokens"] == 50
    assert body["workflow_run_id"] == "wr-fake"
    assert body["request_id"] is not None


async def test_dify_run_query_required(client: httpx.AsyncClient) -> None:
    """``query`` 为空时返回 422。"""
    resp = await client.post("/dify/run", json={"query": ""})
    assert resp.status_code == 422
    assert "query" in resp.text


async def test_dify_run_invalid_json(client: httpx.AsyncClient) -> None:
    """请求体不是合法 JSON 时返回 422。"""
    resp = await client.post(
        "/dify/run",
        content="not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
