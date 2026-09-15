"""探针接口、配置校验与错误信封测试。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from service_fakes import Harness, build_deps

from agent_service import AgentServiceDeps, AgentServiceSettings, create_agent_service_app


async def test_health_reports_ok_when_required_dependencies_ready(harness: Harness) -> None:
    """必需依赖齐备 → /health 200 且 status=ok。"""
    response = await harness.client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "agent-service"
    assert body["version"] == "0.1.0"
    assert body["uptime_seconds"] >= 0
    assert body["request_id"]


async def test_health_stays_alive_when_required_dependency_missing(harness: Harness) -> None:
    """必需依赖挂掉 → /health 仍 200，只把 status 置为 degraded。

    存活探针回答的是「进程还在吗」。把它绑上依赖状态会让编排系统去重启一个
    本身没坏的进程。
    """
    harness.deps.set_dependency("qdrant", ready=False, required=True, detail="connection refused")
    response = await harness.client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


async def test_ready_returns_503_and_names_failed_dependency(harness: Harness) -> None:
    """必需依赖未就绪 → /ready 503，并指出是哪一项。"""
    harness.deps.set_dependency("qdrant", ready=False, required=True, detail="connection refused")
    response = await harness.client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    failed = [item for item in body["dependencies"] if not item["ready"] and item["required"]]
    assert [item["name"] for item in failed] == ["qdrant"]
    assert "connection refused" in failed[0]["detail"]


async def test_ready_stays_ready_when_only_optional_dependency_missing(harness: Harness) -> None:
    """只有可选依赖（MCP）未就绪 → /ready 仍 200。"""
    response = await harness.client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    mcp = next(item for item in body["dependencies"] if item["name"] == "mcp")
    assert mcp["ready"] is False
    assert mcp["required"] is False


async def test_ready_covers_every_dependency_layer(harness: Harness) -> None:
    """探针要覆盖全部依赖层，缺一层就等于把排障推给日志。"""
    body = (await harness.client.get("/ready")).json()
    names = {item["name"] for item in body["dependencies"]}
    assert names == {"session_store", "config", "qdrant", "llm", "workflow", "mcp"}


async def test_unknown_path_uses_unified_error_envelope(harness: Harness) -> None:
    """未知路径也走统一信封，客户端只需实现一套解析逻辑。"""
    response = await harness.client.get("/no-such-path")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "invalid_argument"
    assert error["status_code"] == 404
    assert error["request_id"]


async def test_request_id_is_echoed_in_header_and_body(harness: Harness) -> None:
    """request_id 同时出现在响应头与错误体，便于对齐日志。"""
    response = await harness.client.get("/no-such-path", headers={"X-Request-ID": "trace-me"})
    assert response.headers["x-request-id"] == "trace-me"
    assert response.json()["error"]["request_id"] == "trace-me"


async def test_deps_missing_reports_dependency_unavailable(tmp_path: Path) -> None:
    """生命周期未注册容器时明确报依赖不可用，而不是抛 AttributeError 变成 500。"""
    app = create_agent_service_app(deps=build_deps(tmp_path), version="0.1.0")
    del app.state.deps
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dependency_unavailable"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("max_concurrency", 0), ("request_timeout_seconds", 0)],
)
def test_settings_reject_invalid_values(field_name: str, value: int) -> None:
    """配置非法值在启动前被 Pydantic 拦下，不进入运行期。"""
    with pytest.raises(ValueError, match=field_name):
        AgentServiceSettings(**{field_name: value})


def test_settings_auth_disabled_by_default() -> None:
    """未配置密钥时不启用认证，健康探针永远可访问。"""
    assert AgentServiceSettings().auth_enabled is False


def test_deps_gate_capacity_follows_settings() -> None:
    """并发闸门容量直接取自配置，避免两处各写一个数字。"""
    deps = AgentServiceDeps.create(AgentServiceSettings(max_concurrency=3), version="0.1.0")
    assert deps.gate._value == 3
