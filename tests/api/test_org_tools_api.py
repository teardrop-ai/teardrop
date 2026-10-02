# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

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
    webhook_method="GET",
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
    "webhook_method": "GET",
    "timeout_seconds": 10,
}


# ─── POST /tools ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_tool_success(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.create_org_tool", AsyncMock(return_value=_TOOL))
    monkeypatch.setattr("teardrop.routers.org.tools.invalidate_org_tools_cache", AsyncMock())
    monkeypatch.setattr("teardrop.routers.org.tools.registry.get", MagicMock(return_value=None))

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
    monkeypatch.setattr("teardrop.routers.org.tools.registry.get", MagicMock(return_value=MagicMock()))  # non-None

    resp = await api_client.post("/tools", json=_CREATE_BODY)
    assert resp.status_code == 409
    assert "built-in" in resp.json()["detail"]


@pytest.mark.anyio
async def test_create_tool_name_collision_org(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.registry.get", MagicMock(return_value=None))
    monkeypatch.setattr(
        "teardrop.routers.org.tools.create_org_tool",
        AsyncMock(side_effect=ValueError("Tool 'my_tool' already exists")),
    )

    resp = await api_client.post("/tools", json=_CREATE_BODY)
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_create_tool_invalid_schema(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.registry.get", MagicMock(return_value=None))

    # jsonschema.Draft7Validator.check_schema may or may not reject this;
    # test with clearly invalid schema
    body_bad = {**_CREATE_BODY, "input_schema": {"properties": {"x": {"type": 123}}}}
    resp = await api_client.post("/tools", json=body_bad)
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_create_tool_rejects_post_method(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.registry.get", MagicMock(return_value=None))

    body = {**_CREATE_BODY, "webhook_method": "POST"}
    resp = await api_client.post("/tools", json=body)
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_create_tool_ssrf_url(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.registry.get", MagicMock(return_value=None))

    body = {**_CREATE_BODY, "webhook_url": "http://169.254.169.254/metadata"}
    resp = await api_client.post("/tools", json=body)
    assert resp.status_code == 422
    assert "Unsafe" in resp.json()["detail"] or "webhook" in resp.json()["detail"].lower()


# ─── GET /tools ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_list_tools_empty(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.list_org_tools", AsyncMock(return_value=[]))

    resp = await api_client.get("/tools")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_list_tools_returns_own_org(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.list_org_tools", AsyncMock(return_value=[_TOOL]))

    resp = await api_client.get("/tools")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "my_tool"


@pytest.mark.anyio
async def test_list_tools_excludes_inactive_by_default(api_client, monkeypatch):
    active_tool = _TOOL
    inactive_tool = OrgTool(
        id="tool-inactive",
        org_id="test-org-id",
        name="paused_tool",
        description="A paused custom tool",
        input_schema={"type": "object", "properties": {}, "required": []},
        webhook_url="https://example.com/webhook",
        webhook_method="GET",
        has_auth=False,
        timeout_seconds=10,
        is_active=False,
        created_at=_NOW,
        updated_at=_NOW,
    )

    async def mock_list(org_id, *, active_only=True):
        if active_only:
            return [active_tool]
        return [active_tool, inactive_tool]

    monkeypatch.setattr("teardrop.routers.org.tools.list_org_tools", mock_list)

    # Default (no param) should return only active tools
    resp = await api_client.get("/tools")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "my_tool"


@pytest.mark.anyio
async def test_list_tools_active_only_false_includes_inactive(api_client, monkeypatch):
    active_tool = _TOOL
    inactive_tool = OrgTool(
        id="tool-inactive",
        org_id="test-org-id",
        name="paused_tool",
        description="A paused custom tool",
        input_schema={"type": "object", "properties": {}, "required": []},
        webhook_url="https://example.com/webhook",
        webhook_method="GET",
        has_auth=False,
        timeout_seconds=10,
        is_active=False,
        created_at=_NOW,
        updated_at=_NOW,
    )

    async def mock_list(org_id, *, active_only=True):
        if active_only:
            return [active_tool]
        return [active_tool, inactive_tool]

    monkeypatch.setattr("teardrop.routers.org.tools.list_org_tools", mock_list)

    # Explicit active_only=false should include inactive tools
    resp = await api_client.get("/tools?active_only=false")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    names = {t["name"] for t in data}
    assert "my_tool" in names
    assert "paused_tool" in names


# ─── GET /tools/{tool_id} ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_get_tool_by_id(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.get_org_tool", AsyncMock(return_value=_TOOL))

    resp = await api_client.get("/tools/tool-abc")
    assert resp.status_code == 200
    assert resp.json()["id"] == "tool-abc"


@pytest.mark.anyio
async def test_get_tool_not_found(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.get_org_tool", AsyncMock(return_value=None))

    resp = await api_client.get("/tools/nonexistent")
    assert resp.status_code == 404


# ─── PATCH /tools/{tool_id} ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_update_tool(api_client, monkeypatch):
    updated = OrgTool(**{**_TOOL.model_dump(), "description": "Updated desc"})
    monkeypatch.setattr("teardrop.routers.org.tools.update_org_tool", AsyncMock(return_value=updated))
    monkeypatch.setattr("teardrop.routers.org.tools.invalidate_org_tools_cache", AsyncMock())

    resp = await api_client.patch("/tools/tool-abc", json={"description": "Updated desc"})
    assert resp.status_code == 200
    assert resp.json()["description"] == "Updated desc"


@pytest.mark.anyio
async def test_update_tool_not_found(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.update_org_tool", AsyncMock(return_value=None))

    resp = await api_client.patch("/tools/bad-id", json={"description": "new"})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_update_tool_no_fields(api_client):
    resp = await api_client.patch("/tools/tool-abc", json={})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_create_tool_omitted_price_is_platform_default(api_client, monkeypatch):
    create_mock = AsyncMock(return_value=_TOOL)
    monkeypatch.setattr("teardrop.routers.org.tools.create_org_tool", create_mock)
    monkeypatch.setattr("teardrop.routers.org.tools.invalidate_org_tools_cache", AsyncMock())
    monkeypatch.setattr("teardrop.routers.org.tools.registry.get", MagicMock(return_value=None))

    resp = await api_client.post("/tools", json=_CREATE_BODY)

    assert resp.status_code == 201
    assert create_mock.await_args.kwargs["base_price_usdc"] is None
    assert resp.json()["base_price_usdc"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("price", [None, 0, 2500])
async def test_update_tool_forwards_explicit_price(api_client, monkeypatch, price):
    """Explicit null reverts to the platform default; 0 is free; > 0 is the author price."""
    update_mock = AsyncMock(return_value=OrgTool(**{**_TOOL.model_dump(), "base_price_usdc": price}))
    monkeypatch.setattr("teardrop.routers.org.tools.update_org_tool", update_mock)
    monkeypatch.setattr("teardrop.routers.org.tools.invalidate_org_tools_cache", AsyncMock())

    resp = await api_client.patch("/tools/tool-abc", json={"base_price_usdc": price})

    assert resp.status_code == 200
    assert update_mock.await_args.kwargs["base_price_usdc"] == price
    assert resp.json()["base_price_usdc"] == price


@pytest.mark.anyio
async def test_update_tool_omitted_price_is_not_forwarded(api_client, monkeypatch):
    updated = OrgTool(**{**_TOOL.model_dump(), "description": "Updated desc"})
    update_mock = AsyncMock(return_value=updated)
    monkeypatch.setattr("teardrop.routers.org.tools.update_org_tool", update_mock)
    monkeypatch.setattr("teardrop.routers.org.tools.invalidate_org_tools_cache", AsyncMock())

    resp = await api_client.patch("/tools/tool-abc", json={"description": "Updated desc"})

    assert resp.status_code == 200
    assert "base_price_usdc" not in update_mock.await_args.kwargs


# ─── DELETE /tools/{tool_id} ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_delete_tool(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.delete_org_tool", AsyncMock(return_value=True))
    monkeypatch.setattr("teardrop.routers.org.tools.invalidate_org_tools_cache", AsyncMock())

    resp = await api_client.delete("/tools/tool-abc")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


@pytest.mark.anyio
async def test_delete_tool_not_found(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.delete_org_tool", AsyncMock(return_value=False))

    resp = await api_client.delete("/tools/bad-id")
    assert resp.status_code == 404


# ─── GET /admin/tools/{org_id} ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_admin_list_tools(admin_api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.admin.tools.list_org_tools", AsyncMock(return_value=[_TOOL]))

    resp = await admin_api_client.get("/admin/tools/test-org-id")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1


@pytest.mark.anyio
async def test_admin_list_tools_requires_admin(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.admin.tools.list_org_tools", AsyncMock(return_value=[]))

    resp = await api_client.get("/admin/tools/test-org-id")
    assert resp.status_code == 403


# ─── publish_as_mcp validation ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_tool_publish_requires_author_config(api_client, monkeypatch):
    """POST /tools with publish_as_mcp=true must reject when no settlement wallet is registered."""
    monkeypatch.setattr("teardrop.routers.org.tools.registry.get", MagicMock(return_value=None))
    monkeypatch.setattr(
        "teardrop.routers.org.tools.create_org_tool",
        AsyncMock(
            side_effect=ValueError(
                "Cannot publish tool to marketplace — register a settlement wallet first via POST /marketplace/author-config"
            )
        ),
    )

    body = {
        **_CREATE_BODY,
        "publish_as_mcp": True,
        "output_schema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
        "marketplace_description": "A published tool",
        "base_price_usdc": 1_000_000,
    }
    resp = await api_client.post("/tools", json=body)
    assert resp.status_code == 409
    assert "settlement wallet" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_create_tool_publish_denied_for_read_scope_machine(anon_client, monkeypatch):
    """A read-scoped machine credential cannot publish a community tool."""
    from teardrop.auth import create_access_token

    create = AsyncMock(return_value=_TOOL)
    monkeypatch.setattr("teardrop.routers.org.tools.create_org_tool", create)
    monkeypatch.setattr("teardrop.routers.org.tools.registry.get", MagicMock(return_value=None))
    token = create_access_token(
        "read-key",
        extra_claims={"auth_method": "client_credentials", "org_id": "test-org-id", "scope": "read"},
    )
    body = {
        **_CREATE_BODY,
        "publish_as_mcp": True,
        "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
    }

    resp = await anon_client.post("/tools", json=body, headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403
    create.assert_not_awaited()


@pytest.mark.anyio
async def test_create_tool_publish_denied_for_disabled_machine(anon_client, monkeypatch):
    """A disabled machine credential cannot publish even with the publish scope."""
    import teardrop.users as users
    from teardrop.auth import create_access_token

    monkeypatch.setattr(users, "is_client_credential_disabled", AsyncMock(return_value=True))
    create = AsyncMock(return_value=_TOOL)
    monkeypatch.setattr("teardrop.routers.org.tools.create_org_tool", create)
    monkeypatch.setattr("teardrop.routers.org.tools.registry.get", MagicMock(return_value=None))
    token = create_access_token(
        "disabled-key",
        extra_claims={"auth_method": "client_credentials", "org_id": "test-org-id", "scope": "publish"},
    )
    body = {
        **_CREATE_BODY,
        "publish_as_mcp": True,
        "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
    }

    resp = await anon_client.post("/tools", json=body, headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403
    create.assert_not_awaited()


@pytest.mark.anyio
async def test_create_unpublished_tool_allowed_for_read_scope_machine(anon_client, monkeypatch):
    """Non-publishing tool CRUD stays available to a read-scoped machine credential."""
    from teardrop.auth import create_access_token

    create = AsyncMock(return_value=_TOOL)
    monkeypatch.setattr("teardrop.routers.org.tools.create_org_tool", create)
    monkeypatch.setattr("teardrop.routers.org.tools.invalidate_org_tools_cache", AsyncMock())
    monkeypatch.setattr("teardrop.routers.org.tools.registry.get", MagicMock(return_value=None))
    token = create_access_token(
        "read-key",
        extra_claims={"auth_method": "client_credentials", "org_id": "test-org-id", "scope": "read"},
    )

    resp = await anon_client.post("/tools", json=_CREATE_BODY, headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 201
    create.assert_awaited_once()


_PUBLISHED_TOOL = OrgTool(**{**_TOOL.model_dump(), "publish_as_mcp": True, "is_active": False})


def _machine_headers(scope: str) -> dict[str, str]:
    from teardrop.auth import create_access_token

    token = create_access_token(
        f"{scope}-key",
        extra_claims={"auth_method": "client_credentials", "org_id": "test-org-id", "scope": scope},
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"base_price_usdc": 1_000_000},
        {"webhook_url": "https://example.com/replacement"},
        {"marketplace_description": "Changed listing"},
        {"is_active": True},
        {"publish_as_mcp": False},
    ],
)
async def test_patch_published_tool_denied_for_read_scope_machine(anon_client, monkeypatch, body):
    """Read-scoped machines cannot alter, reactivate, or unpublish an already-published tool."""
    update = AsyncMock(return_value=_PUBLISHED_TOOL)
    monkeypatch.setattr("teardrop.routers.org.tools.get_org_tool", AsyncMock(return_value=_PUBLISHED_TOOL))
    monkeypatch.setattr("teardrop.routers.org.tools.update_org_tool", update)

    resp = await anon_client.patch("/tools/tool-abc", json=body, headers=_machine_headers("read"))

    assert resp.status_code == 403
    update.assert_not_awaited()


@pytest.mark.anyio
async def test_delete_published_tool_denied_for_read_scope_machine(anon_client, monkeypatch):
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr("teardrop.routers.org.tools.get_org_tool", AsyncMock(return_value=_PUBLISHED_TOOL))
    monkeypatch.setattr("teardrop.routers.org.tools.delete_org_tool", delete)

    resp = await anon_client.delete("/tools/tool-abc", headers=_machine_headers("read"))

    assert resp.status_code == 403
    delete.assert_not_awaited()


@pytest.mark.anyio
async def test_published_tool_mutations_allowed_for_publish_scope_machine(anon_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.get_org_tool", AsyncMock(return_value=_PUBLISHED_TOOL))
    monkeypatch.setattr("teardrop.routers.org.tools.update_org_tool", AsyncMock(return_value=_PUBLISHED_TOOL))
    monkeypatch.setattr("teardrop.routers.org.tools.delete_org_tool", AsyncMock(return_value=True))
    monkeypatch.setattr("teardrop.routers.org.tools.invalidate_org_tools_cache", AsyncMock())
    headers = _machine_headers("publish")

    assert (await anon_client.patch("/tools/tool-abc", json={"base_price_usdc": 0}, headers=headers)).status_code == 200
    assert (await anon_client.delete("/tools/tool-abc", headers=headers)).status_code == 200


@pytest.mark.anyio
async def test_private_tool_mutations_allowed_for_read_scope_machine(anon_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.org.tools.get_org_tool", AsyncMock(return_value=_TOOL))
    monkeypatch.setattr("teardrop.routers.org.tools.update_org_tool", AsyncMock(return_value=_TOOL))
    monkeypatch.setattr("teardrop.routers.org.tools.delete_org_tool", AsyncMock(return_value=True))
    monkeypatch.setattr("teardrop.routers.org.tools.invalidate_org_tools_cache", AsyncMock())
    headers = _machine_headers("read")

    assert (await anon_client.patch("/tools/tool-abc", json={"description": "x"}, headers=headers)).status_code == 200
    assert (await anon_client.delete("/tools/tool-abc", headers=headers)).status_code == 200
