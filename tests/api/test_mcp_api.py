"""API tests for MCP server endpoints (CRUD + discover)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from mcp_client import OrgMcpServer

_NOW = datetime.now(timezone.utc)


def _sample_server(**overrides: object) -> OrgMcpServer:
    defaults = {
        "id": "srv-1",
        "org_id": "test-org-id",
        "name": "my_server",
        "url": "https://mcp.example.com/sse",
        "auth_type": "none",
        "has_auth": False,
        "auth_header_name": None,
        "is_active": True,
        "timeout_seconds": 15,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return OrgMcpServer(**defaults)


# ─── POST /mcp/servers ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_mcp_server(api_client, monkeypatch):
    mock_create = AsyncMock(return_value=_sample_server())
    monkeypatch.setattr("teardrop.routers.org.mcp.create_org_mcp_server", mock_create)

    resp = await api_client.post(
        "/mcp/servers",
        json={"name": "my_server", "url": "https://mcp.example.com/sse"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "my_server"
    assert data["has_auth"] is False


@pytest.mark.anyio
async def test_create_mcp_server_with_auth(api_client, monkeypatch):
    mock_create = AsyncMock(return_value=_sample_server(auth_type="bearer", has_auth=True))
    monkeypatch.setattr("teardrop.routers.org.mcp.create_org_mcp_server", mock_create)

    resp = await api_client.post(
        "/mcp/servers",
        json={
            "name": "my_server",
            "url": "https://mcp.example.com/sse",
            "auth_type": "bearer",
            "auth_token": "secret-token",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["has_auth"] is True


@pytest.mark.anyio
async def test_create_mcp_server_header_auth_missing_name(api_client, monkeypatch):
    resp = await api_client.post(
        "/mcp/servers",
        json={
            "name": "my_server",
            "url": "https://mcp.example.com/sse",
            "auth_type": "header",
            "auth_token": "secret-token",
        },
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_create_mcp_server_auth_type_no_token(api_client, monkeypatch):
    resp = await api_client.post(
        "/mcp/servers",
        json={
            "name": "my_server",
            "url": "https://mcp.example.com/sse",
            "auth_type": "bearer",
        },
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_create_mcp_server_value_error(api_client, monkeypatch):
    mock_create = AsyncMock(side_effect=ValueError("MCP server limit reached"))
    monkeypatch.setattr("teardrop.routers.org.mcp.create_org_mcp_server", mock_create)

    resp = await api_client.post(
        "/mcp/servers",
        json={"name": "my_server", "url": "https://mcp.example.com/sse"},
    )
    assert resp.status_code == 422
    assert "limit" in resp.json()["detail"]


@pytest.mark.anyio
async def test_create_mcp_server_requires_auth(anon_client):
    resp = await anon_client.post(
        "/mcp/servers",
        json={"name": "my_server", "url": "https://mcp.example.com/sse"},
    )
    assert resp.status_code == 401


# ─── GET /mcp/servers ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_list_mcp_servers(api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.list_org_mcp_servers",
        AsyncMock(return_value=[_sample_server()]),
    )

    resp = await api_client.get("/mcp/servers")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "my_server"


@pytest.mark.anyio
async def test_list_mcp_servers_empty(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.mcp.list_org_mcp_servers", AsyncMock(return_value=[]))

    resp = await api_client.get("/mcp/servers")
    assert resp.status_code == 200
    assert resp.json() == []


# ─── GET /mcp/servers/{server_id} ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_get_mcp_server(api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.get_org_mcp_server",
        AsyncMock(return_value=_sample_server()),
    )

    resp = await api_client.get("/mcp/servers/srv-1")
    assert resp.status_code == 200
    assert resp.json()["id"] == "srv-1"


@pytest.mark.anyio
async def test_get_mcp_server_not_found(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.mcp.get_org_mcp_server", AsyncMock(return_value=None))

    resp = await api_client.get("/mcp/servers/nonexistent")
    assert resp.status_code == 404


# ─── PATCH /mcp/servers/{server_id} ───────────────────────────────────────────


@pytest.mark.anyio
async def test_patch_mcp_server(api_client, monkeypatch):
    updated = _sample_server(name="renamed")
    monkeypatch.setattr("teardrop.routers.org.mcp.update_org_mcp_server", AsyncMock(return_value=updated))

    resp = await api_client.patch("/mcp/servers/srv-1", json={"name": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed"


@pytest.mark.anyio
async def test_patch_mcp_server_not_found(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.mcp.update_org_mcp_server", AsyncMock(return_value=None))

    resp = await api_client.patch("/mcp/servers/srv-1", json={"name": "renamed"})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_patch_mcp_server_no_fields(api_client, monkeypatch):
    resp = await api_client.patch("/mcp/servers/srv-1", json={})
    assert resp.status_code == 422


# ─── DELETE /mcp/servers/{server_id} ──────────────────────────────────────────


@pytest.mark.anyio
async def test_delete_mcp_server(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.mcp.delete_org_mcp_server", AsyncMock(return_value=True))

    resp = await api_client.delete("/mcp/servers/srv-1")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


@pytest.mark.anyio
async def test_delete_mcp_server_not_found(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.mcp.delete_org_mcp_server", AsyncMock(return_value=False))

    resp = await api_client.delete("/mcp/servers/nonexistent")
    assert resp.status_code == 404


# ─── POST /mcp/servers/{server_id}/discover ───────────────────────────────────


@pytest.mark.anyio
async def test_discover_mcp_tools(api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.get_org_mcp_server",
        AsyncMock(return_value=_sample_server()),
    )
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.discover_mcp_tools_with_schema",
        AsyncMock(
            return_value=(
                [
                    {
                        "name": "add",
                        "description": "Add numbers",
                        "input_schema": {},
                        "output_schema": {"type": "object"},
                    },
                ],
                True,
            )
        ),
    )

    resp = await api_client.post("/mcp/servers/srv-1/discover")
    assert resp.status_code == 200
    data = resp.json()
    assert data["server_id"] == "srv-1"
    assert data["schema_changed"] is True
    assert len(data["tools"]) == 1
    assert data["tools"][0]["name"] == "add"
    assert data["tools"][0]["output_schema"] == {"type": "object"}


@pytest.mark.anyio
async def test_discover_mcp_tools_server_not_found(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.mcp.get_org_mcp_server", AsyncMock(return_value=None))

    resp = await api_client.post("/mcp/servers/nonexistent/discover")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_discover_mcp_tools_rate_limited(api_client, monkeypatch):
    from fastapi import HTTPException

    async def _raise_rate_limit(*args, **kwargs):
        raise HTTPException(status_code=429, detail="Rate limit exceeded for MCP server discovery.")

    monkeypatch.setattr("teardrop.routers.org.mcp._enforce_rate_limit", _raise_rate_limit)
    # get_org_mcp_server must NOT be reached once rate limited.
    get_server = AsyncMock(return_value=_sample_server())
    monkeypatch.setattr("teardrop.routers.org.mcp.get_org_mcp_server", get_server)

    resp = await api_client.post("/mcp/servers/srv-1/discover")
    assert resp.status_code == 429
    get_server.assert_not_called()


@pytest.mark.anyio
async def test_discover_mcp_tools_connection_error(api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.get_org_mcp_server",
        AsyncMock(return_value=_sample_server()),
    )
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.discover_mcp_tools_with_schema",
        AsyncMock(side_effect=ConnectionError("refused")),
    )

    resp = await api_client.post("/mcp/servers/srv-1/discover")
    assert resp.status_code == 502


# ─── GET /admin/mcp/servers/{org_id} ──────────────────────────────────────────


@pytest.mark.anyio
async def test_admin_list_mcp_servers(admin_api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.admin.tools.list_org_mcp_servers",
        AsyncMock(return_value=[_sample_server()]),
    )

    resp = await admin_api_client.get("/admin/mcp/servers/test-org-id")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1


# ─── POST /mcp/servers/{server_id}/test-tool ──────────────────────────────────


def _fake_lc_tool(ainvoke_return):
    """Return a fake StructuredTool-like object with a controllable ainvoke."""
    fake = AsyncMock()
    fake.ainvoke = AsyncMock(return_value=ainvoke_return)
    return fake


@pytest.mark.anyio
async def test_test_mcp_tool_happy(api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.get_org_mcp_server",
        AsyncMock(return_value=_sample_server()),
    )
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.discover_mcp_tools",
        AsyncMock(
            return_value=[
                {"name": "add", "description": "Add", "input_schema": {}, "output_schema": None},
            ]
        ),
    )
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.build_mcp_backed_tool",
        lambda *a, **kw: _fake_lc_tool({"sum": 3}),
    )

    resp = await api_client.post(
        "/mcp/servers/srv-1/test-tool",
        json={"tool_name": "add", "args": {"a": 1, "b": 2}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["result"] == {"sum": 3}
    assert data["error"] is None
    assert data["latency_ms"] >= 0


@pytest.mark.anyio
async def test_test_mcp_tool_server_not_found(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.mcp.get_org_mcp_server", AsyncMock(return_value=None))

    resp = await api_client.post(
        "/mcp/servers/nonexistent/test-tool",
        json={"tool_name": "add", "args": {}},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_test_mcp_tool_tool_not_found(api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.get_org_mcp_server",
        AsyncMock(return_value=_sample_server()),
    )
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.discover_mcp_tools",
        AsyncMock(return_value=[{"name": "other", "description": "", "input_schema": {}}]),
    )

    resp = await api_client.post(
        "/mcp/servers/srv-1/test-tool",
        json={"tool_name": "add", "args": {}},
    )
    assert resp.status_code == 404
    assert "add" in resp.json()["detail"]


@pytest.mark.anyio
async def test_test_mcp_tool_connection_error(api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.get_org_mcp_server",
        AsyncMock(return_value=_sample_server()),
    )
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.discover_mcp_tools",
        AsyncMock(side_effect=ConnectionError("refused")),
    )

    resp = await api_client.post(
        "/mcp/servers/srv-1/test-tool",
        json={"tool_name": "add", "args": {}},
    )
    assert resp.status_code == 502


@pytest.mark.anyio
async def test_test_mcp_tool_rate_limited(api_client, monkeypatch):
    from fastapi import HTTPException

    async def _raise_rate_limit(*args, **kwargs):
        raise HTTPException(status_code=429, detail="Rate limit exceeded for MCP tool test.")

    monkeypatch.setattr("teardrop.routers.org.mcp._enforce_rate_limit", _raise_rate_limit)
    get_server = AsyncMock(return_value=_sample_server())
    monkeypatch.setattr("teardrop.routers.org.mcp.get_org_mcp_server", get_server)

    resp = await api_client.post(
        "/mcp/servers/srv-1/test-tool",
        json={"tool_name": "add", "args": {}},
    )
    assert resp.status_code == 429
    get_server.assert_not_called()


@pytest.mark.anyio
async def test_test_mcp_tool_upstream_error_payload(api_client, monkeypatch):
    """When the wrapped tool returns {'error': ...} without raising, success=False."""
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.get_org_mcp_server",
        AsyncMock(return_value=_sample_server()),
    )
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.discover_mcp_tools",
        AsyncMock(return_value=[{"name": "add", "description": "Add", "input_schema": {}}]),
    )
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.build_mcp_backed_tool",
        lambda *a, **kw: _fake_lc_tool({"error": "upstream timeout"}),
    )

    resp = await api_client.post(
        "/mcp/servers/srv-1/test-tool",
        json={"tool_name": "add", "args": {}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["error"] == "upstream timeout"
    assert data["result"] is None


@pytest.mark.anyio
async def test_test_mcp_tool_invocation_exception(api_client, monkeypatch):
    """When ainvoke raises, success=False with a type-name error (no secrets)."""
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.get_org_mcp_server",
        AsyncMock(return_value=_sample_server()),
    )
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.discover_mcp_tools",
        AsyncMock(return_value=[{"name": "add", "description": "Add", "input_schema": {}}]),
    )

    fake = AsyncMock()
    fake.ainvoke = AsyncMock(side_effect=ValueError("bad args"))
    monkeypatch.setattr(
        "teardrop.routers.org.mcp.build_mcp_backed_tool",
        lambda *a, **kw: fake,
    )

    resp = await api_client.post(
        "/mcp/servers/srv-1/test-tool",
        json={"tool_name": "add", "args": {}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "ValueError" in data["error"]
    assert data["result"] is None
