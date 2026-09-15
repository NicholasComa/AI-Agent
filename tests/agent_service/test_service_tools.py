"""MCP 工具路由测试：清单、调用、工具级错误与依赖不可用。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from service_fakes import (
    FakeCallResult,
    FakeContent,
    FakeMcpSession,
    FakeTool,
    Harness,
    ServiceChat,
    build_deps,
)

from agent_service import create_agent_service_app

_PING_OK = FakeCallResult(
    structuredContent={"message": "pong"},
    content=[FakeContent(text="pong")],
)
_LIST_OK = FakeCallResult(
    structuredContent={"ok": True, "total": 2, "items": ["README.md", "notes.txt"]},
    content=[FakeContent(text="2 items")],
)


@pytest.fixture
async def mcp_harness(tmp_path: Path, chunk_id: str) -> AsyncIterator[Harness]:
    """启用了 MCP（注入会话替身）的夹具。"""
    session = FakeMcpSession(
        tools=[
            FakeTool("ping", "连通性检查", {"type": "object", "properties": {}}),
            FakeTool("list_files", "列出沙箱文件", {"type": "object"}),
        ],
        results={"ping": _PING_OK, "list_files": _LIST_OK},
    )
    chat = ServiceChat(chunk_id)
    deps = build_deps(tmp_path, chat=chat, mcp=session)
    app = create_agent_service_app(deps=deps, version="0.1.0")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield Harness(client=client, deps=deps, chat=chat)


async def test_list_tools_returns_catalog(mcp_harness: Harness) -> None:
    """工具清单带名称、说明与入参 schema，供调用方构造参数。"""
    response = await mcp_harness.client.get("/v1/tools")
    assert response.status_code == 200
    body = response.json()
    names = [tool["name"] for tool in body["tools"]]
    assert names == ["ping", "list_files"]
    assert body["tools"][0]["description"] == "连通性检查"
    assert body["tools"][1]["input_schema"] == {"type": "object"}
    assert body["request_id"]


async def test_call_tool_returns_structured_envelope(mcp_harness: Harness) -> None:
    """工具调用回传结构化信封与文本内容。"""
    response = await mcp_harness.client.post(
        "/v1/tools/list_files/call",
        json={"arguments": {"path": "."}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["is_error"] is False
    assert body["structured"]["ok"] is True
    assert body["structured"]["total"] == 2
    assert body["text"] == "2 items"
    session = mcp_harness.deps.mcp
    assert session.calls == [("list_files", {"path": "."})]


async def test_tool_level_error_is_not_http_error(mcp_harness: Harness) -> None:
    """工具自身返回的失败是业务结果：HTTP 200 + is_error=true。

    混成 5xx 会让调用方无法区分「我的参数错了」和「服务挂了」。
    """
    response = await mcp_harness.client.post(
        "/v1/tools/no_such_tool/call",
        json={"arguments": {}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["is_error"] is True
    assert body["structured"]["ok"] is False


async def test_tools_unavailable_when_mcp_disabled(harness: Harness) -> None:
    """未启用 MCP 时返回 503 与稳定错误码，而不是 500。"""
    listing = await harness.client.get("/v1/tools")
    assert listing.status_code == 503
    assert listing.json()["error"]["code"] == "dependency_unavailable"

    call = await harness.client.post("/v1/tools/ping/call", json={"arguments": {}})
    assert call.status_code == 503
    assert call.json()["error"]["code"] == "dependency_unavailable"


async def test_tool_call_rejects_invalid_payload(mcp_harness: Harness) -> None:
    """arguments 必须是对象；未知字段被拒。"""
    response = await mcp_harness.client.post(
        "/v1/tools/ping/call",
        json={"arguments": "not-an-object"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_argument"
