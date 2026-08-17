"""文本向量化。

提供两种实现：

- :class:`FakeEmbedding`：**离线、确定性**的哈希向量化（纯 Python，无网络），
  用于本地原型与单元测试 —— 相同文本永远得到相同向量，相似的文本得到相近的
  向量（余弦相似度高）。
- :class:`EmbeddingClient`：调用 OpenAI 兼容的 ``POST /embeddings`` 接口，
  从环境变量读取 ``EMBEDDING_API_BASE_URL`` / ``EMBEDDING_MODEL_NAME`` /
  ``EMBEDDING_API_KEY``。Ollama 在该端口（默认 11434）提供
  ``/v1/embeddings`` 作为 OpenAI 兼容接口（无 key）。

:func:`get_embedding` 是工厂：当环境变量里配置了真实接口时返回
:class:`EmbeddingClient`，否则回退到 :class:`FakeEmbedding`，保证离线也能跑通。

两者都满足 :class:`Embedder` 协议：``embed(texts) -> list[list[float]]``。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from typing import Any, Protocol

import httpx

DEFAULT_DIM = 1024  # mxbai-embed-large 等常用 Ollama Embedding 模型的默认维度

# 分词：按 Unicode 单词（连续字母/数字）切分，小写化。中文字符按单字切分。
_TOKEN_RE = re.compile(r"[\w]+|[\u4e00-\u9fff]")

# 不可重试的 HTTP 状态码（鉴权/参数错误/资源不存在等）。
_NON_RETRYABLE_STATUS = {400, 401, 403, 404, 422}


class EmbeddingError(RuntimeError):
    """Embedding 调用错误基类。"""


class EmbeddingAuthError(EmbeddingError):
    """鉴权失败（401/403），不应重试。"""


class EmbeddingClientError(EmbeddingError):
    """请求参数错误（400/404/422 等），不应重试。"""


class EmbeddingTransientError(EmbeddingError):
    """临时性失败（429/5xx/网络/超时），按配置重试。"""


def _bucket(token: str, dim: int) -> tuple[int, float]:
    """把 token 稳定映射到一个桶下标与符号（hashing trick）。

    使用 ``hashlib.md5`` 而非内置 ``hash()``，因为内置 ``hash(str)`` 受
    ``PYTHONHASHSEED`` 影响、跨进程不稳定；md5 保证跨运行确定性，便于评测复现。
    """
    digest = hashlib.md5(token.encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "big") % dim
    sign = 1.0 if digest[4] % 2 == 0 else -1.0
    return index, sign


class Embedder(Protocol):
    """嵌入器协议：输入文本列表，输出等长向量列表。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """把若干文本映射为向量。"""
        ...

    @property
    def dim(self) -> int:
        """向量维度。"""
        ...


class FakeEmbedding:
    """确定性的哈希向量化（离线）。

    对每个 token 做 hashing trick 累加，再 L2 归一化，使向量成为单位向量，
    从而点积即余弦相似度。适合离线原型与单元测试。
    """

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        if dim <= 0:
            msg = f"dim must be > 0, got {dim}"
            raise ValueError(msg)
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN_RE.findall(text.lower()):
            index, sign = _bucket(token, self.dim)
            vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec  # 空文本：返回零向量
        return [v / norm for v in vec]


class EmbeddingClient:
    """OpenAI 兼容 ``/embeddings`` 接口的同步客户端。

    读取 :func:`get_embedding` 传入或环境变量提供的配置；密钥绝不写入日志或异常。

    重试策略：仅对 :class:`EmbeddingTransientError` 重试（指数退避），
    鉴权/参数错误（:class:`EmbeddingAuthError` / :class:`EmbeddingClientError`）
    立即抛出。``max_retries=0`` 表示不重试。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url:
            msg = "base_url is required"
            raise ValueError(msg)
        if not model:
            msg = "model is required"
            raise ValueError(msg)
        if max_retries < 0:
            msg = f"max_retries must be >= 0, got {max_retries}"
            raise ValueError(msg)
        if api_key is None:
            api_key = os.environ.get("EMBEDDING_API_KEY", "")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._dim: int | None = None  # 第一次成功调用后确定

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        """向量维度（首次成功调用后可知）。"""
        if self._dim is None:
            msg = "dim unknown: call embed() at least once before reading .dim"
            raise RuntimeError(msg)
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {"model": self._model, "input": texts}
        data = self._request_with_retry("/embeddings", payload)
        try:
            items = data["data"]
        except (KeyError, TypeError) as exc:
            msg = f"missing 'data' in embedding response: {exc}"
            raise EmbeddingClientError(msg) from exc
        vectors: list[list[float]] = []
        for item in items:
            try:
                vector = item["embedding"]
            except (KeyError, TypeError) as exc:
                msg = f"missing 'embedding' in response item: {exc}"
                raise EmbeddingClientError(msg) from exc
            if not isinstance(vector, list):
                msg = f"'embedding' must be a list, got {type(vector).__name__}"
                raise EmbeddingClientError(msg)
            vectors.append([float(x) for x in vector])
        if vectors and self._dim is None:
            self._dim = len(vectors[0])
        return vectors

    def _request_with_retry(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_exc: EmbeddingError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._request_once(path, payload)
            except EmbeddingTransientError as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                time.sleep(self._backoff * (2 ** attempt))
        assert last_exc is not None
        raise last_exc

    def _request_once(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = self._client.post(f"{self._base_url}{path}", json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            msg = f"embedding request timeout after {self._client.timeout}s"
            raise EmbeddingTransientError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"embedding request network error: {type(exc).__name__}"
            raise EmbeddingTransientError(msg) from exc

        status = response.status_code
        if status in (401, 403):
            raise EmbeddingAuthError(f"embedding auth failed: http {status}")
        if status in (400, 404, 422):
            raise EmbeddingClientError(f"embedding request rejected: http {status}")
        if status == 429 or status >= 500:
            raise EmbeddingTransientError(f"embedding transient failure: http {status}")
        if status != 200:
            raise EmbeddingError(f"embedding request failed: http {status}")
        try:
            result: dict[str, Any] = response.json()
        except ValueError as exc:
            raise EmbeddingClientError(f"invalid JSON in embedding response: {exc}") from exc
        return result


def get_embedding(dim: int = DEFAULT_DIM) -> Embedder:
    """根据环境变量返回嵌入器。

    - 配置了 ``EMBEDDING_API_BASE_URL`` 且 ``EMBEDDING_MODEL_NAME`` → 真实客户端；
    - 否则回退到离线 :class:`FakeEmbedding`（默认，保证无需网络即可跑通）。
    """
    base_url = os.environ.get("EMBEDDING_API_BASE_URL", "")
    model = os.environ.get("EMBEDDING_MODEL_NAME", "")
    if base_url and model:
        max_retries = int(os.environ.get("EMBEDDING_MAX_RETRIES", "2"))
        backoff = float(os.environ.get("EMBEDDING_BACKOFF_SECONDS", "0.5"))
        return EmbeddingClient(
            base_url=base_url, model=model, max_retries=max_retries, backoff_seconds=backoff
        )
    return FakeEmbedding(dim=dim)
