"""Dify Workflow REST API 异步客户端。

本模块把 Dify 工作流封装成可复用的 Python 客户端，供 FastAPI
路由调用。设计上与具体工作流解耦：只负责把 `query` 送进 Dify 并取回
`data.outputs`；工作流输出字段的解析交给
调用方 / Pydantic 响应模型。

环境变量
--------
在 `.env` 中配置：
  DIFY_API_KEY=<工作流级密钥，从 Dify Studio → API 访问复制>
  DIFY_BASE_URL=http://localhost/v1   # 本地自托管；Cloud 用 https://api.dify.ai/v1

用法
----
    from src.dify_client import DifyWorkflowClient

    client = DifyWorkflowClient()
    result = await client.run("需要在用户登录页面添加手机号验证码登录功能，要求验证码 5 分钟内有效，错误次数 3 次锁定 1 小时；登录成功后跳转到首页。")
    print(result.outputs)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from dotenv import load_dotenv

DEFAULT_BASE_URL = "http://localhost/v1"


@dataclass
class DifyRunResult:
    """Dify Workflow blocking 调用的精简结果。"""

    query: str
    outputs: dict[str, Any]
    workflow_run_id: str | None = None
    task_id: str | None = None
    status: str | None = None
    elapsed_time: float | None = None
    total_tokens: int | None = None
    raw: dict[str, Any] | None = None


class DifyWorkflowClient:
    """调用 Dify Workflow 的异步 httpx 客户端。

    Attributes:
        api_key: Dify 工作流级 API Key。
        base_url: Dify API base URL，末尾不带 `/`。
        timeout: 单次请求超时（秒）。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        load_dotenv()
        self.api_key = (api_key if api_key is not None else os.getenv("DIFY_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("DIFY_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.timeout = timeout

        if not self.api_key:
            msg = (
                "DIFY_API_KEY 未配置。请在 .env 中设置 workflow 级 API Key\n"
                "获取路径：Dify Studio → 打开应用 → 发布 → API 访问 → 复制密钥"
            )
            raise ValueError(msg)

    async def run(
        self,
        query: str,
        *,
        user: str = "default",
        response_mode: str = "blocking",
    ) -> DifyRunResult:
        """调用工作流并返回精简结果。

        Args:
            query: 工作流 `开始` 节点的 `query` 输入。
            user: Dify 要求的调用方标识。
            response_mode: "blocking" 或 "streaming"；本阶段先支持 blocking。

        Returns:
            DifyRunResult，其中 `outputs` 即 `data.outputs`（字段由所调用的工作流决定）。

        Raises:
            httpx.HTTPStatusError: Dify 返回非 2xx。
            httpx.ConnectError: 网络不可达（如本地 Dify 没启动）。
            httpx.TimeoutException: 请求超时。
        """
        url = f"{self.base_url}/workflows/run"
        payload = {
            "inputs": {"query": query},
            "response_mode": response_mode,
            "user": user,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()

        data = body.get("data", {})
        return DifyRunResult(
            query=query,
            outputs=data.get("outputs") or {},
            workflow_run_id=body.get("workflow_run_id"),
            task_id=body.get("task_id"),
            status=data.get("status"),
            elapsed_time=data.get("elapsed_time"),
            total_tokens=data.get("total_tokens"),
            raw=body,
        )
