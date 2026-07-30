"""Day 7 - 模型客户端工厂。

把 :class:`AppConfig` 转换为具体 :class:`ModelClient` 实例的入口。
目前唯一实现是 :class:`LlmClient`（OpenAI 兼容 + SSE 流式）；未来
可通过 ``settings.provider`` 分发到不同的 client 类。

为什么要有这个工厂
------------------

1. **依赖注入点。** 上层（FastAPI lifespan、CLI、测试 fixture）只调
   :func:`build_model_client`,不直接 ``import LlmClient``,便于在测试中
   替换为 fake。
2. **配置即代码。** ``AppConfig`` 已经是单一可信来源；切换模型只改
   ``MODEL_NAME`` / ``API_BASE_URL`` / ``API_KEY``,业务路由代码（``app.py``
   的所有 ``@app.get/post``）一行不动。这正是 Roadmap「切换模型只改配置,
   不改业务接口」通过标准的实现。
3. **provider 字段预留。** 当前 ``provider="openai_compatible"`` 字段
   已存在（Day 6 加）但不分发；后续可在此处 ``if settings.provider ==
   "ollama": return OllamaClient(...)``。
"""

from __future__ import annotations

from config import AppConfig
from llm_client import LlmClient
from model_client import ModelClient


def build_model_client(settings: AppConfig) -> ModelClient:
    """根据 :class:`AppConfig` 构造一个可用的 :class:`ModelClient`。

    解析规则:
        * ``api_key``: 若 ``settings.api_key`` 为空字符串,降级为 ``None``,
          让 :class:`LlmClient` 内部走 ``os.environ.get("API_KEY")`` 兜底
          (与 week01 行为一致)。
        * ``timeout_seconds`` / ``max_retries`` / ``retry_backoff``: 直接
          透传给客户端,所有重试行为按 ``AppConfig`` 配置驱动。

    Args:
        settings: 已校验的 :class:`AppConfig` 实例。

    Returns:
        一个就绪可用的 :class:`ModelClient`（当前唯一实现是 :class:`LlmClient`）。

    Raises:
        llm_client.LlmAuthError: ``settings.api_key`` 为空且 ``API_KEY``
            环境变量也未设置时构造阶段即抛错。
    """
    return LlmClient(
        base_url=settings.api_base_url,
        model=settings.model_name,
        api_key=settings.api_key or None,
        timeout_seconds=settings.timeout_seconds,
        max_retries=settings.max_retries,
        retry_backoff=settings.retry_backoff,
        max_concurrency=settings.max_concurrency,
    )
