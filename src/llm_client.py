"""Day 3 - asynchronous LLM chat-completions client.

The client is a thin async wrapper around an OpenAI-compatible
``POST /chat/completions`` endpoint. It focuses on three things:

1. **Robustness.** Classify failures (auth, rate limit, server, timeout,
   malformed response) into distinct exceptions and retry only the
   transient ones (429 / 5xx / connect / read timeout).
2. **Key safety.** The API key is read from the ``API_KEY`` environment
   variable by default and is never written to logs, exception messages,
   or response bodies.
3. **Testability.** The underlying ``httpx.AsyncClient`` can be injected
   so tests can use ``httpx.MockTransport`` instead of real HTTP.

Example
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
            result = await llm.chat([{"role": "user", "content": "你好"}])
            print(result.text, result.model, result.elapsed_ms)


    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LlmError(Exception):
    """Base class for all LlmClient errors."""


class LlmAuthError(LlmError):
    """401/403 - the API key is missing, invalid, or unauthorized."""


class LlmRateLimitError(LlmError):
    """429 - rate limited; raised only after retries are exhausted."""


class LlmServerError(LlmError):
    """5xx - server-side failure; raised only after retries are exhausted."""


class LlmTimeoutError(LlmError):
    """Connect or read timeout; raised only after retries are exhausted."""


class LlmResponseFormatError(LlmError):
    """The response body is not a valid chat-completion payload."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LlmResult:
    """Successful chat-completion result.

    Attributes:
        text: the assistant's reply.
        model: the model name echoed by the server (may differ from the
            request when the gateway falls back to a default model).
        elapsed_ms: wall-clock duration of the call, including any retries.
        usage: optional token-usage dict from the server
            (e.g. ``{"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}``).
    """

    text: str
    model: str
    elapsed_ms: int
    usage: dict[str, int] | None = None

    def __repr__(self) -> str:
        # ``text`` is truncated so a long reply doesn't blow up logs.
        snippet = self.text[:40] + ("..." if len(self.text) > 40 else "")
        return f"LlmResult(model={self.model!r}, elapsed_ms={self.elapsed_ms}, text={snippet!r})"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
"""HTTP status codes that should trigger a retry with exponential backoff.

We retry 429 (rate limit) and 5xx (server hiccup); 408 means "we were too
slow uploading", which is also worth another shot. Everything else
(400/401/403/404/...) is a client mistake and would fail the same way again.
"""

DEFAULT_MAX_RETRIES = 2
"""Default number of retry attempts after the first failure (0 = no retry)."""

DEFAULT_RETRY_BACKOFF = 0.5
"""Base backoff in seconds. Real delay is ``base * 2 ** attempt``."""

DEFAULT_TIMEOUT = 30.0
"""Default per-request timeout in seconds."""


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class LlmClient:
    """Asynchronous chat-completions client.

    The client owns a private :class:`httpx.AsyncClient` unless one is
    injected via ``client=`` (used by tests with ``httpx.MockTransport``).
    The instance is also an async context manager::

        async with LlmClient(base_url=..., model=...) as llm:
            result = await llm.chat([...])

    Args:
        base_url: OpenAI-compatible base URL (no trailing slash).
        model: default model identifier.
        api_key: explicit key. If ``None`` (default), read from env.
        env_var: name of the env var to read when ``api_key`` is ``None``.
        timeout_seconds: per-request timeout.
        max_retries: how many times to retry on transient failure.
        retry_backoff: base backoff in seconds; real delay is
            ``retry_backoff * 2 ** attempt``.
        client: optional :class:`httpx.AsyncClient` (used by tests).
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

        # API key resolution: explicit arg wins, otherwise env.
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
        """Close the owned ``httpx.AsyncClient`` (no-op if injected)."""
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
        """Send a chat-completions request and return the parsed result.

        Args:
            messages: non-empty list of ``{"role": ..., "content": ...}`` dicts.
            model: optional per-call override of the default model.
            extra_body: optional fields merged into the JSON body
                (e.g. ``{"temperature": 0.2}``).

        Returns:
            :class:`LlmResult` with text, model, elapsed_ms, and (if the
            server returned one) usage.

        Raises:
            LlmAuthError: 401/403, or missing API key.
            LlmRateLimitError: 429 after all retries exhausted.
            LlmServerError: 5xx after all retries exhausted.
            LlmTimeoutError: connect/read timeout after all retries exhausted.
            LlmResponseFormatError: response is not a valid chat-completion.
            LlmError: any other 4xx (e.g. 400 bad request, 404 not found).
            ValueError: ``messages`` is empty.
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
                # Never include the URL/key in this message; the body itself
                # is enough for diagnosis.
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

            # Non-200 path: log status + brief body, NEVER the request headers
            # (so the API key can never leak through logs).
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

            # Other 4xx (400, 404, ...): client error, no retry.
            raise LlmError(f"http {status}: {body_snippet}")

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = self._retry_backoff * (2**attempt)
        await asyncio.sleep(delay)

    def _redact(self, text: str) -> str:
        """Replace the API key with ``[REDACTED]`` to keep it out of logs/excs.

        Defends against a buggy or malicious server that echoes our key
        in the response body, an upstream error wrapper, or a proxy
        that appends request headers to its error page.
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
