"""Day 19 — ``src.dify_client`` 单元测试。

用 ``unittest.mock`` 替换 ``httpx.AsyncClient``，不发出真实 HTTP 请求。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dify_client import DifyWorkflowClient


@pytest.fixture
def mock_post_response() -> MagicMock:
    """伪造一个 httpx Response。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "workflow_run_id": "wr-123",
        "task_id": "task-456",
        "data": {
            "status": "succeeded",
            "outputs": {"result": {"answer": "1261.5"}},
            "elapsed_time": 1.23,
            "total_tokens": 100,
        },
    }
    resp.raise_for_status.return_value = None
    return resp


async def test_run_success(mock_post_response: MagicMock) -> None:
    """正常调用返回 DifyRunResult，字段全部解析正确。"""
    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post.return_value = mock_post_response

    with patch("dify_client.httpx.AsyncClient", return_value=fake_client):
        client = DifyWorkflowClient(api_key="app-test", base_url="http://localhost/v1")
        result = await client.run("算一下 1+1")

    assert result.query == "算一下 1+1"
    assert result.outputs == {"result": {"answer": "1261.5"}}
    assert result.status == "succeeded"
    assert result.elapsed_time == 1.23
    assert result.total_tokens == 100
    assert result.workflow_run_id == "wr-123"
    assert result.task_id == "task-456"

    # 校验请求体
    call_args = fake_client.post.call_args
    assert call_args.kwargs["json"]["inputs"] == {"query": "算一下 1+1"}
    assert call_args.kwargs["json"]["response_mode"] == "blocking"
    assert call_args.kwargs["headers"]["Authorization"] == "Bearer app-test"


async def test_missing_api_key() -> None:
    """未配置 API Key 时初始化直接抛 ValueError。"""
    with pytest.raises(ValueError, match="DIFY_API_KEY"):
        DifyWorkflowClient(api_key="", base_url="http://localhost/v1")


async def test_default_base_url() -> None:
    """不传 base_url 时使用本地默认值。"""
    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"data": {"outputs": {}}},
        raise_for_status=lambda: None,
    )

    with patch("dify_client.httpx.AsyncClient", return_value=fake_client):
        client = DifyWorkflowClient(api_key="app-test")
        await client.run("hello")

    assert client.base_url == "http://localhost/v1"
    call_url = fake_client.post.call_args.args[0]
    assert call_url == "http://localhost/v1/workflows/run"
