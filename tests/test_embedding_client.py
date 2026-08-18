"""EmbeddingClient 单元测试。

覆盖：1 正常 + 3+ 异常（鉴权、客户端错误、瞬时错误/超时、响应缺字段）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from rag.embeddings import (
    EmbeddingAuthError,
    EmbeddingClient,
    EmbeddingClientError,
    EmbeddingTransientError,
    FakeEmbedding,
    get_embedding,
)


def _mock_response(status_code: int, body: dict | str) -> httpx.Response:
    content = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body.encode("utf-8")
    return httpx.Response(status_code, content=content, request=httpx.Request("POST", "http://x"))


def test_embedding_client_success_records_dim() -> None:
    """正常：200 返回两个向量后，.dim 应等于向量长度。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _mock_response(
            200,
            {"data": [{"embedding": [0.1, 0.2, 0.3]}, {"embedding": [0.4, 0.5, 0.6]}]},
        )

    transport = httpx.MockTransport(handler)
    client = EmbeddingClient(
        base_url="http://x", model="m", client=httpx.Client(transport=transport), max_retries=0
    )
    vecs = client.embed(["a", "b"])
    assert client.dim == 3
    assert [v[0] for v in vecs] == pytest.approx([0.1, 0.4])
    assert captured["body"]["model"] == "m"
    assert captured["body"]["input"] == ["a", "b"]


def test_embedding_client_auth_error_401_no_retry() -> None:
    """异常 1：401 → EmbeddingAuthError，不重试。"""

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _mock_response(401, {"error": "unauthorized"})

    transport = httpx.MockTransport(handler)
    client = EmbeddingClient(
        base_url="http://x", model="m", client=httpx.Client(transport=transport), max_retries=3
    )
    with pytest.raises(EmbeddingAuthError):
        client.embed(["a"])
    assert calls["n"] == 1  # 不重试


def test_embedding_client_param_error_400_no_retry() -> None:
    """异常 2：400 → EmbeddingClientError，不重试。"""

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _mock_response(400, {"error": "bad request"})

    transport = httpx.MockTransport(handler)
    client = EmbeddingClient(
        base_url="http://x", model="m", client=httpx.Client(transport=transport), max_retries=3
    )
    with pytest.raises(EmbeddingClientError):
        client.embed(["a"])
    assert calls["n"] == 1


def test_embedding_client_transient_500_retries_and_succeeds() -> None:
    """异常 3：500 → EmbeddingTransientError，按配置重试到成功。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return _mock_response(500, {"error": "boom"})
        return _mock_response(200, {"data": [{"embedding": [0.1, 0.2]}]})

    transport = httpx.MockTransport(handler)
    client = EmbeddingClient(
        base_url="http://x",
        model="m",
        client=httpx.Client(transport=transport),
        max_retries=3,
        backoff_seconds=0.0,
    )
    vecs = client.embed(["a"])
    assert calls["n"] == 3  # 2 次失败 + 第 3 次成功
    assert vecs == [[0.1, 0.2]]


def test_embedding_client_timeout_raises_transient_no_retry_when_zero() -> None:
    """异常 4：httpx.TimeoutException → EmbeddingTransientError（max_retries=0 时立即抛）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    transport = httpx.MockTransport(handler)
    client = EmbeddingClient(
        base_url="http://x", model="m", client=httpx.Client(transport=transport), max_retries=0
    )
    with pytest.raises(EmbeddingTransientError):
        client.embed(["a"])


def test_embedding_client_missing_data_field_raises_client_error() -> None:
    """异常 5：响应缺 'data' 字段 → EmbeddingClientError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _mock_response(200, {"unexpected": "shape"})

    transport = httpx.MockTransport(handler)
    client = EmbeddingClient(
        base_url="http://x", model="m", client=httpx.Client(transport=transport), max_retries=0
    )
    with pytest.raises(EmbeddingClientError):
        client.embed(["a"])


def test_get_embedding_falls_back_to_fake_when_env_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_embedding：未配置 EMBEDDING_* env 时回退到 FakeEmbedding。"""
    monkeypatch.delenv("EMBEDDING_API_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL_NAME", raising=False)
    emb = get_embedding(dim=8)
    assert isinstance(emb, FakeEmbedding)
    assert emb.dim == 8
