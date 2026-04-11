"""API tests for custom tool endpoints (POST/GET/PATCH/DELETE /tools, GET /admin/tools)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from org_tools import OrgTool

_NOW = datetime.now(timezone.utc)

_TOOL = OrgTool(
    id="tool-abc",
    org_id="test-org-id",
    name="my_tool",
    description="A test custom tool",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    webhook_url="https://example.com/webhook",
    webhook_method="POST",
    has_auth=False,
    timeout_seconds=10,
    is_active=True,
    created_at=_NOW,
    updated_at=_NOW,
)

_CREATE_BODY = {
    "name": "my_tool",
    "description": "A test custom tool",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    "webhook_url": "https://example.com/webhook",
    "webhook_method": "POST",
    "timeout_seconds": 10,
}


# ─── POST /tools ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_tool_success(api_client, monkeypatch):
    monkeypatch.setattr("app.create_org_tool", AsyncMock(return_value=_TOOL))
    monkeypatch.setattr("app.invalidate_org_tools_cache", AsyncMock())
    monkeypatch.setattr("app.registry.get", MagicMock(return_value=None))

    resp = await api_client.post("/tools", json=_CREATE_BODY)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "my_tool"
    assert data["org_id"] == "test-org-id"


@pytest.mark.anyio
async def test_create_tool_unauthenticated(anon_client):
    resp = await anon_client.post("/tools", json=_CREATE_BODY)
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_create_tool_name_collision_global(api_client, monkeypatch):
    monkeypatch.setattr("app.registry.get", MagicMock(return_value=MagicMock()))  # non-None

    resp = await api_client.post("/tools", json=_CREATE_BODY)
    assert resp.status_code == 409
    assert "built-in" in resp.json()["detail"]


@pytest.mark.anyio
async def test_create_tool_name_collision_org(api_client, monkeypatch):
    monkeypatch.setattr("app.registry.get", MagicMock(return_value=None))
    monkeypatch.setattr(
        "app.create_org_tool",
        AsyncMock(side_effect=ValueError("Tool 'my_tool' already exists")),
    )

    resp = await api_client.post("/tools", json=_CREATE_BODY)
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_create_tool_invalid_schema(api_client, monkeypatch):
    monkeypatch.setattr("app.registry.get", MagicMock(return_value=None))

    # jsonschema.Draft7Validator.check_schema may or may not reject this;
    # test with clearly invalid schema
    body_bad = {**_CREATE_BODY, "input_schema": {"properties": {"x": {"type": 123}}}}
    resp = await api_client.post("/tools", json=body_bad)
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_create_tool_ssrf_url(api_client, monkeypatch):
    monkeypatch.setattr("app.registry.get", MagicMock(return_value=None))

    body = {**_CREATE_BODY, "webhook_url": "http://169.254.169.254/metadata"}
    resp = await api_client.post("/tools", json=body)
    assert resp.status_code == 422
    assert "Unsafe" in resp.json()["detail"] or "webhook" in resp.json()["detail"].lower()


# ─── GET /tools ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_list_tools_empty(api_client, monkeypatch):
    monkeypatch.setattr("app.list_org_tools", AsyncMock(return_value=[]))

    resp = await api_client.get("/tools")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_list_tools_returns_own_org(api_client, monkeypatch):
    monkeypatch.setattr("app.list_org_tools", AsyncMock(return_value=[_TOOL]))

    resp = await api_client.get("/tools")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "my_tool"


# ─── GET /tools/{tool_id} ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_get_tool_by_id(api_client, monkeypatch):
    monkeypatch.setattr("app.get_org_tool", AsyncMock(return_value=_TOOL))

    resp = await api_client.get("/tools/tool-abc")
    assert resp.status_code == 200
    assert resp.json()["id"] == "tool-abc"


@pytest.mark.anyio
async def test_get_tool_not_found(api_client, monkeypatch):
    monkeypatch.setattr("app.get_org_tool", AsyncMock(return_value=None))

    resp = await api_client.get("/tools/nonexistent")
    assert resp.status_code == 404


# ─── PATCH /tools/{tool_id} ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_update_tool(api_client, monkeypatch):
    updated = OrgTool(**{**_TOOL.model_dump(), "description": "Updated desc"})
    monkeypatch.setattr("app.update_org_tool", AsyncMock(return_value=updated))
    monkeypatch.setattr("app.invalidate_org_tools_cache", AsyncMock())

    resp = await api_client.patch("/tools/tool-abc", json={"description": "Updated desc"})
    assert resp.status_code == 200
    assert resp.json()["description"] == "Updated desc"


@pytest.mark.anyio
async def test_update_tool_not_found(api_client, monkeypatch):
    monkeypatch.setattr("app.update_org_tool", AsyncMock(return_value=None))

    resp = await api_client.patch("/tools/bad-id", json={"description": "new"})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_update_tool_no_fields(api_client):
    resp = await api_client.patch("/tools/tool-abc", json={})
    assert resp.status_code == 422


# ─── DELETE /tools/{tool_id} ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_delete_tool(api_client, monkeypatch):
    monkeypatch.setattr("app.delete_org_tool", AsyncMock(return_value=True))
    monkeypatch.setattr("app.invalidate_org_tools_cache", AsyncMock())

    resp = await api_client.delete("/tools/tool-abc")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


@pytest.mark.anyio
async def test_delete_tool_not_found(api_client, monkeypatch):
    monkeypatch.setattr("app.delete_org_tool", AsyncMock(return_value=False))

    resp = await api_client.delete("/tools/bad-id")
    assert resp.status_code == 404


# ─── GET /admin/tools/{org_id} ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_admin_list_tools(admin_api_client, monkeypatch):
    monkeypatch.setattr("app.list_org_tools", AsyncMock(return_value=[_TOOL]))

    resp = await admin_api_client.get("/admin/tools/test-org-id")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1


@pytest.mark.anyio
async def test_admin_list_tools_requires_admin(api_client, monkeypatch):
    monkeypatch.setattr("app.list_org_tools", AsyncMock(return_value=[]))

    resp = await api_client.get("/admin/tools/test-org-id")
    assert resp.status_code == 403
