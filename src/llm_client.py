"""Day 3 + Day 7 - 异步 LLM 对话补全客户端（流式 + 协议实现）。

该客户端是对一个 OpenAI 兼容的 ``POST /chat/completions`` 接口的一层
轻量异步封装。Day 7 升级:

1. **SSE 流式输出**。新增 :meth:`LlmClient.chat_stream` —— 解析 ``data: {...}``
   增量,逐个 ``yield`` ``delta.content``,遇 ``data: [DONE]`` 停止。
2. **协议实现**。:class:`LlmClient` 自动满足 :class:`model_client.ModelClient`
   协议（结构子类型,无需继承）。后续路由层、fake 实现都基于该协议。

它仍然专注三件事：

1. **健壮性。** 把各类失败（鉴权、限流、服务端、超时、响应格式错误）
   归类为不同的异常，并且只对临时性失败（429 / 5xx / 连接 / 读取超时）
   做重试。
2. **密钥安全。** API 密钥默认从 ``API_KEY`` 环境变量读取，绝不写入日志、
   异常消息或响应体。
3. **可测试性。** 底层的 ``httpx.AsyncClient`` 可以注入，方便测试用
   ``httpx.MockTransport`` 替代真实 HTTP。

示例
-------
::

    import asyncio
    from src.config import load_config
    from src.llm_client import LlmClient


    async def main() -> None:
        cfg = load_config()
        async with LlmClient(
            base_url=cfg.api_base_url,
            model=cfg.model_name,
            timeout_seconds=cfg.timeout_seconds,
        ) as llm:
            # 一次性调用
            result = await llm.chat([{"role": "user", "content": "你好"}])
            print(result.text, result.model, result.elapsed_ms)

            # 流式调用
            async for delta in llm.chat_stream([{"role": "user", "content": "写首短诗"}]):
                print(delta, end="", flush=True)


    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class LlmError(Exception):
    """所有 LlmClient 错误的基类。"""


class LlmAuthError(LlmError):
    """401/403 - API 密钥缺失、无效或未授权。"""


class LlmRateLimitError(LlmError):
    """429 - 触发限流；仅在重试全部耗尽后才抛出。"""


class LlmServerError(LlmError):
    """5xx - 服务端失败；仅在重试全部耗尽后才抛出。"""


class LlmTimeoutError(LlmError):
    """连接或读取超时；仅在重试全部耗尽后才抛出。"""


class LlmResponseFormatError(LlmError):
    """响应体不是一个合法的对话补全结果。"""


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LlmResult:
    """一次成功的对话补全结果。

    Attributes:
        text: 模型的回复文本。
        model: 服务端回显的模型名（当网关回退到默认模型时，可能与请求的不同）。
        elapsed_ms: 调用所花的墙上时钟时间，包含任何重试。
        usage: 服务端返回的可选 token 用量字典
            （例如 ``{"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}``）。
    """

    text: str
    model: str
    elapsed_ms: int
    usage: dict[str, int] | None = None

    def __repr__(self) -> str:
        # ``text`` 做截断，避免超长回复把日志撑爆。
        snippet = self.text[:40] + ("..." if len(self.text) > 40 else "")
        return f"LlmResult(model={self.model!r}, elapsed_ms={self.elapsed_ms}, text={snippet!r})"


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
"""应当触发指数退避重试的 HTTP 状态码。

重试 429（限流）和 5xx（服务端抖动）；408 表示「我们上传太慢」，
也值得再试一次。其它（400/401/403/404/...）都是客户端错误，
重试也会得到同样的结果。
"""

DEFAULT_MAX_RETRIES = 2
"""首次失败后默认的额外重试次数（0 表示不重试）。"""

DEFAULT_RETRY_BACKOFF = 0.5
"""退避基准秒数。实际延迟为 ``base * 2 ** attempt``。"""

DEFAULT_TIMEOUT = 30.0
"""每次请求的默认超时（秒）。"""


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class LlmClient:
    """异步对话补全客户端。

    客户端自身持有一个私有的 :class:`httpx.AsyncClient`，除非通过 ``client=``
    注入一个（测试用 ``httpx.MockTransport`` 时就是注入）。该实例同时也是一个
    异步上下文管理器::

        async with LlmClient(base_url=..., model=...) as llm:
            result = await llm.chat([...])

    Args:
        base_url: OpenAI 兼容的 base URL（不带结尾斜杠）。
        model: 默认模型标识。
        api_key: 显式传入的密钥。如果为 ``None``（默认），则从环境变量读取。
        env_var: 当 ``api_key`` 为 ``None`` 时读取的环境变量名。
        timeout_seconds: 每次请求的超时。
        max_retries: 遇到临时失败时重试的次数。
        retry_backoff: 退避基准秒数；实际延迟为
            ``retry_backoff * 2 ** attempt``。
        client: 可选注入的 :class:`httpx.AsyncClient`（用于测试）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        env_var: str = "API_KEY",
        timeout_seconds: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            msg = "base_url is required"
            raise ValueError(msg)
        if not model:
            msg = "model is required"
            raise ValueError(msg)
        if max_retries < 0:
            msg = "max_retries must be >= 0"
            raise ValueError(msg)

        # API 密钥解析：显式传入优先，否则读环境变量。
        if api_key is None:
            api_key = os.environ.get(env_var)
        if not api_key:
            msg = f"API key not found (set {env_var} or pass api_key=)"
            raise LlmAuthError(msg)
        self._api_key = api_key
        self._env_var = env_var

        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def timeout_seconds(self) -> float:
        return self._timeout

    async def aclose(self) -> None:
        """关闭自身持有的 :class:`httpx.AsyncClient`（若客户端是注入的则为空操作）。"""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> LlmClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> LlmResult:
        """发起一次对话补全请求并返回解析后的结果。

        Args:
            messages: 非空的 ``{"role": ..., "content": ...}`` 字典列表。
            model: 可选，单次调用时覆盖默认模型。
            extra_body: 可选，合并进 JSON body 的额外字段
                （例如 ``{"temperature": 0.2}``）。

        Returns:
            :class:`LlmResult`，包含 text、model、elapsed_ms，以及（若服务端
            返回了的话）usage。

        Raises:
            LlmAuthError: 401/403，或缺失 API 密钥。
            LlmRateLimitError: 重试耗尽后的 429。
            LlmServerError: 重试耗尽后的 5xx。
            LlmTimeoutError: 重试耗尽后的连接/读取超时。
            LlmResponseFormatError: 响应不是合法的对话补全结果。
            LlmError: 任何其它 4xx（例如 400 错误请求、404 未找到）。
            ValueError: ``messages`` 为空。
        """
        if not messages:
            msg = "messages must be a non-empty list"
            raise ValueError(msg)

        url = f"{self._base_url}/chat/completions"
        used_model = model or self._model
        body: dict[str, Any] = {
            "model": used_model,
            "messages": list(messages),
        }
        if extra_body:
            body.update(extra_body)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        attempt = 0
        while True:
            started = time.perf_counter()
            try:
                response = await self._client.post(url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                elapsed_ms = _elapsed_ms(started)
                # 该消息里绝不带入 URL/密钥；光看 body 本身已足够诊断。
                logger.warning(
                    "llm timeout attempt=%d elapsed_ms=%d err=%s",
                    attempt + 1,
                    elapsed_ms,
                    exc,
                )
                if attempt < self._max_retries:
                    await self._sleep_backoff(attempt)
                    attempt += 1
                    continue
                msg = f"request timed out after {attempt + 1} attempts"
                raise LlmTimeoutError(msg) from exc
            except httpx.ConnectError as exc:
                elapsed_ms = _elapsed_ms(started)
                logger.warning(
                    "llm connect error attempt=%d elapsed_ms=%d err=%s",
                    attempt + 1,
                    elapsed_ms,
                    exc,
                )
                if attempt < self._max_retries:
                    await self._sleep_backoff(attempt)
                    attempt += 1
                    continue
                msg = f"connection failed after {attempt + 1} attempts"
                raise LlmTimeoutError(msg) from exc

            elapsed_ms = _elapsed_ms(started)
            status = response.status_code

            if status == 200:
                return self._parse_response(response, elapsed_ms, used_model)

            # 非 200 分支：记录状态码和简短的 body，但绝不记录请求头
            # （这样密钥就绝不会通过日志泄漏）。
            body_snippet = self._redact((response.text or "")[:200])
            logger.warning(
                "llm http=%d attempt=%d elapsed_ms=%d body=%s",
                status,
                attempt + 1,
                elapsed_ms,
                body_snippet,
            )

            if status in (401, 403):
                raise LlmAuthError(f"authentication failed ({status}): {body_snippet}")

            if status in RETRYABLE_STATUS and attempt < self._max_retries:
                await self._sleep_backoff(attempt)
                attempt += 1
                continue

            if status == 429:
                raise LlmRateLimitError(f"rate limited: {body_snippet}")
            if 500 <= status < 600:
                raise LlmServerError(f"server error {status}: {body_snippet}")

            # 其它 4xx（400、404 等）：客户端错误，不重试。
            raise LlmError(f"http {status}: {body_snippet}")

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """发起一次流式对话补全，逐个 yield 增量文本片段。

        通过 OpenAI 兼容接口的 ``stream: true`` + SSE（``text/event-stream``）
        协议获取增量回复。SSE 格式::

            data: {"choices":[{"delta":{"content":"你"}}]}\\n\\n
            data: {"choices":[{"delta":{"content":"好"}}]}\\n\\n
            data: [DONE]\\n\\n

        本方法对每个非空 ``delta.content`` 调一次 ``yield``。

        **重试边界（重要）**：仅在"打开流连接"阶段（连接错误、读取超时、
        非 200 状态码）按 ``max_retries`` 整体重试。一旦成功开始 ``yield``
        delta，则不再重试——因为已有内容已交付消费者，整体重试会重复。

        Args:
            messages: 非空的 ``{"role": ..., "content": ...}`` 字典列表。
            model: 可选，单次调用时覆盖默认模型。
            extra_body: 可选，合并进 JSON body 的额外字段
                （例如 ``{"temperature": 0.2}``）。

        Yields:
            每段增量文本。

        Raises:
            LlmAuthError: 401/403（不重试）。
            LlmRateLimitError: 重试耗尽后的 429。
            LlmServerError: 重试耗尽后的 5xx。
            LlmTimeoutError: 重试耗尽后的连接/读取超时。
            LlmResponseFormatError: SSE 事件 JSON 非法或形状不对。
            LlmError: 任何其它 4xx（例如 400/404）。
            ValueError: ``messages`` 为空。
        """
        if not messages:
            msg = "messages must be a non-empty list"
            raise ValueError(msg)

        url = f"{self._base_url}/chat/completions"
        used_model = model or self._model
        body: dict[str, Any] = {
            "model": used_model,
            "messages": list(messages),
            "stream": True,
        }
        if extra_body:
            body.update(extra_body)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        attempt = 0
        while True:
            started = time.perf_counter()
            try:
                async with self._client.stream(
                    "POST",
                    url,
                    json=body,
                    headers=headers,
                ) as response:
                    elapsed_ms = _elapsed_ms(started)
                    status = response.status_code

                    if status != 200:
                        # 把错误响应体完整读出来用于日志/异常
                        await response.aread()
                        body_snippet = self._redact((response.text or "")[:200])
                        logger.warning(
                            "llm stream http=%d attempt=%d elapsed_ms=%d body=%s",
                            status,
                            attempt + 1,
                            elapsed_ms,
                            body_snippet,
                        )

                        if status in (401, 403):
                            raise LlmAuthError(f"authentication failed ({status}): {body_snippet}")

                        if status in RETRYABLE_STATUS and attempt < self._max_retries:
                            await self._sleep_backoff(attempt)
                            attempt += 1
                            continue

                        if status == 429:
                            raise LlmRateLimitError(f"rate limited: {body_snippet}")
                        if 500 <= status < 600:
                            raise LlmServerError(f"server error {status}: {body_snippet}")

                        raise LlmError(f"http {status}: {body_snippet}")

                    # 200：开始按行解析 SSE
                    async for line in response.aiter_lines():
                        # SSE 注释行 / 心跳 / 空行：跳过
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data: "):
                            continue

                        payload = line[len("data: ") :]
                        if payload == "[DONE]":
                            return

                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError as exc:
                            msg = f"SSE event is not valid JSON: {exc}"
                            raise LlmResponseFormatError(msg) from exc

                        try:
                            delta = event["choices"][0]["delta"].get("content", "")
                        except (KeyError, IndexError, TypeError) as exc:
                            msg = f"missing 'choices[0].delta.content' in SSE event: {exc}"
                            raise LlmResponseFormatError(msg) from exc

                        if not isinstance(delta, str):
                            msg = f"'delta.content' must be a string, got {type(delta).__name__}"
                            raise LlmResponseFormatError(msg)

                        if delta:
                            yield delta

                    return  # 流正常结束
            except httpx.TimeoutException as exc:
                elapsed_ms = _elapsed_ms(started)
                logger.warning(
                    "llm stream timeout attempt=%d elapsed_ms=%d err=%s",
                    attempt + 1,
                    elapsed_ms,
                    exc,
                )
                if attempt < self._max_retries:
                    await self._sleep_backoff(attempt)
                    attempt += 1
                    continue
                msg = f"stream timed out after {attempt + 1} attempts"
                raise LlmTimeoutError(msg) from exc
            except httpx.ConnectError as exc:
                elapsed_ms = _elapsed_ms(started)
                logger.warning(
                    "llm stream connect error attempt=%d elapsed_ms=%d err=%s",
                    attempt + 1,
                    elapsed_ms,
                    exc,
                )
                if attempt < self._max_retries:
                    await self._sleep_backoff(attempt)
                    attempt += 1
                    continue
                msg = f"stream connect failed after {attempt + 1} attempts"
                raise LlmTimeoutError(msg) from exc

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = self._retry_backoff * (2**attempt)
        await asyncio.sleep(delay)

    def _redact(self, text: str) -> str:
        """把 API 密钥替换成 ``[REDACTED]``，避免它进入日志或异常。

        防御对象是：有 bug 或恶意的服务器在我们的响应体里原样回显密钥、
        上游错误包装器、或把请求头附到错误页上的代理。
        """
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, "[REDACTED]")
        return text

    def _parse_response(
        self,
        response: httpx.Response,
        elapsed_ms: int,
        requested_model: str,
    ) -> LlmResult:
        try:
            data = response.json()
        except Exception as exc:
            msg = f"response is not valid JSON: {exc}"
            raise LlmResponseFormatError(msg) from exc

        if not isinstance(data, dict):
            msg = f"response top-level must be an object, got {type(data).__name__}"
            raise LlmResponseFormatError(msg)

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            msg = f"missing 'choices[0].message.content' in response: {exc}"
            raise LlmResponseFormatError(msg) from exc

        if not isinstance(content, str):
            msg = f"'content' must be a string, got {type(content).__name__}"
            raise LlmResponseFormatError(msg)

        model = data.get("model")
        if not isinstance(model, str):
            model = requested_model

        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = None

        return LlmResult(
            text=content,
            model=model,
            elapsed_ms=elapsed_ms,
            usage=usage,
        )
