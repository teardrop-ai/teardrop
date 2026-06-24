from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import teardrop.config as config
from mcp_client import OrgMcpServer
from org_tools import OrgTool

_NOW = datetime.now(timezone.utc)


@pytest.fixture
def marketplace_enabled(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _server(**overrides: object) -> OrgMcpServer:
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


def _created_tool(name: str = "remote_tool") -> OrgTool:
    return OrgTool(
        id="tool-1",
        org_id="test-org-id",
        name=name,
        description="Imported tool",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        output_schema={"type": "object", "properties": {}},
        webhook_url=None,
        webhook_method="GET",
        mcp_server_id="srv-1",
        mcp_tool_name="remote_tool",
        has_auth=False,
        timeout_seconds=15,
        is_active=True,
        publish_as_mcp=True,
        marketplace_description="Imported tool",
        category="",
        base_price_usdc=12_345,
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.mark.anyio
async def test_preview_marketplace_import_member_returns_flags(api_client, monkeypatch, marketplace_enabled):
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setattr("teardrop.routers.marketplace.get_org_mcp_server", AsyncMock(return_value=_server()))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.discover_mcp_tools",
        AsyncMock(
            return_value=[
                {
                    "name": "Remote Tool",
                    "description": "Lookup customer data",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": ["string", "null"], "format": "uri", "minLength": 3}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                    "output_schema": None,
                }
            ]
        ),
    )
    monkeypatch.setattr("teardrop.routers.marketplace.list_org_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.get_current_pricing",
        AsyncMock(return_value=SimpleNamespace(tool_call_cost=12_345)),
    )
    # Member role + no author config → both blockers surfaced.
    monkeypatch.setattr("teardrop.routers.marketplace.get_author_config", AsyncMock(return_value=None))

    resp = await api_client.post("/marketplace/import/preview", json={"server_id": "srv-1"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["server_id"] == "srv-1"
    assert data["slots_remaining"] == config.get_settings().max_org_tools
    assert data["can_publish"] is False
    assert "requires_org_admin" in data["blockers"]
    assert "settlement_wallet_missing" in data["blockers"]
    item = data["tools"][0]
    assert item["remote_tool_name"] == "Remote Tool"
    assert item["proposed_name"] == "remote_tool"
    assert item["schema_status"] == {"input": "normalized", "output": "synthesized"}
    assert item["name_adjusted"] is True
    assert item["name_collision_resolved"] is False
    assert item["quota_exceeded"] is False
    assert item["suggested_base_price_usdc"] == 12_345
    assert item["output_schema"]["type"] == "object"
    assert data["errors"] == []


@pytest.mark.anyio
async def test_preview_marketplace_import_admin_with_wallet_can_publish(admin_api_client, monkeypatch, marketplace_enabled):
    """Admin role + registered settlement wallet → can_publish=True, no blockers."""
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setattr("teardrop.routers.marketplace.get_org_mcp_server", AsyncMock(return_value=_server()))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.discover_mcp_tools",
        AsyncMock(return_value=[{"name": "t", "description": "d", "input_schema": {}, "output_schema": None}]),
    )
    monkeypatch.setattr("teardrop.routers.marketplace.list_org_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.get_current_pricing",
        AsyncMock(return_value=SimpleNamespace(tool_call_cost=12_345)),
    )
    monkeypatch.setattr(
        "teardrop.routers.marketplace.get_author_config",
        AsyncMock(return_value=SimpleNamespace(org_id="test-org-id", settlement_wallet="0x" + "a" * 40)),
    )

    resp = await admin_api_client.post("/marketplace/import/preview", json={"server_id": "srv-1"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["can_publish"] is True
    assert data["blockers"] == []


@pytest.mark.anyio
async def test_preview_marketplace_import_admin_missing_wallet(admin_api_client, monkeypatch, marketplace_enabled):
    """Admin role but no settlement wallet → settlement_wallet_missing blocker."""
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setattr("teardrop.routers.marketplace.get_org_mcp_server", AsyncMock(return_value=_server()))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.discover_mcp_tools",
        AsyncMock(return_value=[{"name": "t", "description": "d", "input_schema": {}, "output_schema": None}]),
    )
    monkeypatch.setattr("teardrop.routers.marketplace.list_org_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.get_current_pricing",
        AsyncMock(return_value=SimpleNamespace(tool_call_cost=12_345)),
    )
    monkeypatch.setattr("teardrop.routers.marketplace.get_author_config", AsyncMock(return_value=None))

    resp = await admin_api_client.post("/marketplace/import/preview", json={"server_id": "srv-1"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["can_publish"] is False
    assert data["blockers"] == ["settlement_wallet_missing"]


@pytest.mark.anyio
async def test_publish_marketplace_import_admin_partial_success(admin_api_client, monkeypatch, marketplace_enabled):
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setattr("teardrop.routers.marketplace.get_org_mcp_server", AsyncMock(return_value=_server()))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.discover_mcp_tools",
        AsyncMock(
            return_value=[
                {"name": "remote_tool", "description": "First", "input_schema": {}, "output_schema": None},
                {"name": "second_tool", "description": "Second", "input_schema": {}, "output_schema": None},
            ]
        ),
    )
    monkeypatch.setattr("teardrop.routers.marketplace.registry.get", MagicMock(return_value=None))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.create_org_tool",
        AsyncMock(
            side_effect=[
                _created_tool("remote_tool"),
                ValueError("Tool 'second_tool' already exists for this organisation"),
            ]
        ),
    )

    resp = await admin_api_client.post(
        "/marketplace/import/publish",
        json={
            "server_id": "srv-1",
            "tools": [
                {
                    "remote_tool_name": "remote_tool",
                    "name": "remote_tool",
                    "description": "First",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                    "base_price_usdc": 10,
                },
                {
                    "remote_tool_name": "second_tool",
                    "name": "second_tool",
                    "description": "Second",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                    "base_price_usdc": 10,
                },
            ],
        },
    )

    assert resp.status_code == 201
    data = resp.json()
    assert len(data["created"]) == 1
    assert data["created"][0]["tool"]["mcp_server_id"] == "srv-1"
    assert len(data["errors"]) == 1
    assert data["errors"][0]["status_code"] == 409


@pytest.mark.anyio
async def test_publish_marketplace_import_derives_missing_schemas(admin_api_client, monkeypatch, marketplace_enabled):
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setattr("teardrop.routers.marketplace.get_org_mcp_server", AsyncMock(return_value=_server()))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.discover_mcp_tools",
        AsyncMock(
            return_value=[
                {
                    "name": "remote_tool",
                    "description": "Lookup customer data",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": ["string", "null"], "format": "uri"}},
                        "required": ["query"],
                    },
                    "output_schema": None,
                }
            ]
        ),
    )
    monkeypatch.setattr("teardrop.routers.marketplace.registry.get", MagicMock(return_value=None))
    create_mock = AsyncMock(return_value=_created_tool("remote_tool"))
    monkeypatch.setattr("teardrop.routers.marketplace.create_org_tool", create_mock)

    resp = await admin_api_client.post(
        "/marketplace/import/publish",
        json={
            "server_id": "srv-1",
            "tools": [
                {
                    "remote_tool_name": "remote_tool",
                    "name": "remote_tool",
                    "description": "Lookup customer data",
                    "base_price_usdc": 10,
                }
            ],
        },
    )

    assert resp.status_code == 201
    create_kwargs = create_mock.await_args.kwargs
    assert create_kwargs["input_schema"] == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    assert create_kwargs["output_schema"] == {
        "type": "object",
        "properties": {},
        "description": "Lookup customer data",
    }


@pytest.mark.anyio
async def test_publish_marketplace_import_forbidden_for_member(api_client, monkeypatch, marketplace_enabled):
    create_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.create_org_tool", create_mock)

    resp = await api_client.post(
        "/marketplace/import/publish",
        json={
            "server_id": "srv-1",
            "tools": [
                {
                    "remote_tool_name": "remote_tool",
                    "name": "remote_tool",
                    "description": "First",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                }
            ],
        },
    )

    assert resp.status_code == 403
    create_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_publish_marketplace_import_requires_author_config(admin_api_client, monkeypatch, marketplace_enabled):
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setattr("teardrop.routers.marketplace.get_org_mcp_server", AsyncMock(return_value=_server()))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.discover_mcp_tools",
        AsyncMock(return_value=[{"name": "remote_tool", "description": "First", "input_schema": {}, "output_schema": None}]),
    )
    monkeypatch.setattr("teardrop.routers.marketplace.registry.get", MagicMock(return_value=None))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.create_org_tool",
        AsyncMock(
            side_effect=ValueError(
                "Cannot publish tool to marketplace — register a settlement wallet first via POST /marketplace/author-config"
            )
        ),
    )

    resp = await admin_api_client.post(
        "/marketplace/import/publish",
        json={
            "server_id": "srv-1",
            "tools": [
                {
                    "remote_tool_name": "remote_tool",
                    "name": "remote_tool",
                    "description": "First",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                }
            ],
        },
    )

    assert resp.status_code == 409
    data = resp.json()
    assert data["created"] == []
    assert data["errors"][0]["status_code"] == 409


@pytest.mark.anyio
async def test_publish_marketplace_import_quota_error(admin_api_client, monkeypatch, marketplace_enabled):
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setattr("teardrop.routers.marketplace.get_org_mcp_server", AsyncMock(return_value=_server()))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.discover_mcp_tools",
        AsyncMock(return_value=[{"name": "remote_tool", "description": "First", "input_schema": {}, "output_schema": None}]),
    )
    monkeypatch.setattr("teardrop.routers.marketplace.registry.get", MagicMock(return_value=None))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.create_org_tool",
        AsyncMock(side_effect=ValueError("Organisation tool limit reached (50)")),
    )

    resp = await admin_api_client.post(
        "/marketplace/import/publish",
        json={
            "server_id": "srv-1",
            "tools": [
                {
                    "remote_tool_name": "remote_tool",
                    "name": "remote_tool",
                    "description": "First",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                }
            ],
        },
    )

    assert resp.status_code == 422
    assert resp.json()["errors"][0]["status_code"] == 422


@pytest.mark.anyio
async def test_publish_marketplace_import_name_collision(admin_api_client, monkeypatch, marketplace_enabled):
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setattr("teardrop.routers.marketplace.get_org_mcp_server", AsyncMock(return_value=_server()))
    monkeypatch.setattr(
        "teardrop.routers.marketplace.discover_mcp_tools",
        AsyncMock(return_value=[{"name": "remote_tool", "description": "First", "input_schema": {}, "output_schema": None}]),
    )
    monkeypatch.setattr("teardrop.routers.marketplace.registry.get", MagicMock(return_value=MagicMock()))
    create_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.create_org_tool", create_mock)

    resp = await admin_api_client.post(
        "/marketplace/import/publish",
        json={
            "server_id": "srv-1",
            "tools": [
                {
                    "remote_tool_name": "remote_tool",
                    "name": "remote_tool",
                    "description": "First",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                }
            ],
        },
    )

    assert resp.status_code == 409
    assert resp.json()["errors"][0]["status_code"] == 409
    create_mock.assert_not_awaited()
