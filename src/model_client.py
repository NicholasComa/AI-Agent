"""Day 7 - 模型客户端统一协议。

把"如何调用一个模型"抽象为一个 :class:`ModelClient` 协议。任何满足
该协议的具体实现都可以挂到 FastAPI ``app.state.llm`` 上使用 - 当前
唯一实现是 :class:`llm_client.LlmClient`，未来可加 :class:`OllamaClient`
等不同 provider。

协议设计目标
------------

1. **结构子类型。** 用 :class:`typing.Protocol` + :func:`typing.runtime_checkable`，
   不强制继承。:class:`LlmClient` 不需要 ``class LlmClient(ModelClient)``；
   只要签名匹配就算实现。这让测试可以用任意 fake 替代。
2. **同步方法与异步方法并存。** ``chat`` / ``aclose`` 是 ``async``；
   ``chat_stream`` 返回 :class:`AsyncIterator`，是普通方法（async generator
   函数本身就是 ``AsyncIterator``）。
3. **结果类型复用。** :class:`LlmResult` 从 :mod:`llm_client` 直接导入，
   不重复定义；这避免了协议层与实现层的循环依赖。

为什么需要这个协议
------------------

* Week 02 的测试经常需要 fake 出 LlmClient 行为（错误分类、重试边界）。
  没有协议层时,所有 fake 都得继承同一个具体类,容易把测试细节泄漏到
  生产代码。
* Day 9 加 :class:`StreamingResponse` 时,路由层不关心底层是 HTTP 拉取
  还是 SSE 推送,只看协议。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from llm_client import LlmResult


@runtime_checkable
class ModelClient(Protocol):
    """模型客户端的统一接口契约。

    Attributes:
        model: 默认模型标识。单次调用可通过 ``chat(..., model=...)`` 覆盖。
        base_url: OpenAI 兼容 base URL。
        timeout_seconds: 每次请求的超时秒数。
    """

    model: str
    base_url: str
    timeout_seconds: float

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> LlmResult:
        """发起一次对话补全请求并返回解析后的结果。"""
        ...

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """发起一次流式对话补全,逐个 yield 增量文本。

        返回 :class:`AsyncIterator[str]`,每个元素是一段增量文本
        (``{"choices":[{"delta":{"content":"..."}}]}`` 中 ``content`` 的值)。
        """
        ...

    async def aclose(self) -> None:
        """释放客户端持有的资源(主要是 :class:`httpx.AsyncClient`)。"""
        ...
