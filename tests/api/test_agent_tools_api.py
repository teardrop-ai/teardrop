"""API tests for GET /agent/tools inventory endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import teardrop.config as config
from marketplace import MarketplaceTool
from org_tools import OrgTool

_NOW = datetime.now(timezone.utc)


def _make_org_tool(name: str, *, is_active: bool = True) -> OrgTool:
    return OrgTool(
        id=f"tool-{name}",
        org_id="test-org-id",
        name=name,
        description=f"Tool {name}",
        input_schema={"type": "object", "properties": {}},
        webhook_url="https://example.com/webhook",
        webhook_method="GET",
        has_auth=False,
        timeout_seconds=10,
        is_active=is_active,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_platform_tool() -> MarketplaceTool:
    return MarketplaceTool(
        name="web_search",
        qualified_name="platform/web_search",
        display_name="Web Search",
        description="Search the web",
        marketplace_description="Search the web",
        input_schema={"type": "object"},
        cost_usdc=15000,
        author_org_name="Teardrop",
        author_org_slug="platform",
    )


def _make_subscribed_tool() -> MarketplaceTool:
    return MarketplaceTool(
        name="weather",
        qualified_name="acme/weather",
        display_name="weather",
        description="Weather tool",
        marketplace_description="Weather lookup",
        input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
        cost_usdc=5000,
        author_org_name="Acme",
        author_org_slug="acme",
    )


@pytest.mark.anyio
async def test_agent_tools_marketplace_enabled(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    config.get_settings.cache_clear()

    monkeypatch.setattr("teardrop.main.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.main.get_current_pricing", AsyncMock(return_value=MagicMock(tool_call_cost=1000)))
    monkeypatch.setattr("teardrop.main.get_marketplace_catalog", AsyncMock(return_value=[_make_platform_tool()]))
    monkeypatch.setattr("teardrop.main.get_subscribed_tools_catalog", AsyncMock(return_value=[_make_subscribed_tool()]))
    monkeypatch.setattr("teardrop.main.list_org_tools", AsyncMock(return_value=[_make_org_tool("my_tool")]))

    resp = await api_client.get("/agent/tools")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    assert len(tools) == 3

    by_qualified = {t["qualified_name"]: t for t in tools}
    assert by_qualified["platform/web_search"]["source"] == "platform"
    assert by_qualified["platform/web_search"]["access_mode"] == "included"
    assert by_qualified["org/my_tool"]["source"] == "org"
    assert by_qualified["org/my_tool"]["access_mode"] == "included"
    assert by_qualified["acme/weather"]["source"] == "marketplace"
    assert by_qualified["acme/weather"]["access_mode"] == "subscribed"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_agent_tools_marketplace_disabled_returns_org_only(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "false")
    config.get_settings.cache_clear()

    monkeypatch.setattr("teardrop.main.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.main.get_current_pricing", AsyncMock(return_value=MagicMock(tool_call_cost=1000)))
    platform_mock = AsyncMock(return_value=[_make_platform_tool()])
    subscribed_mock = AsyncMock(return_value=[_make_subscribed_tool()])
    monkeypatch.setattr("teardrop.main.get_marketplace_catalog", platform_mock)
    monkeypatch.setattr("teardrop.main.get_subscribed_tools_catalog", subscribed_mock)
    monkeypatch.setattr("teardrop.main.list_org_tools", AsyncMock(return_value=[_make_org_tool("my_tool")]))

    resp = await api_client.get("/agent/tools")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    assert len(tools) == 1
    assert tools[0]["qualified_name"] == "org/my_tool"
    assert tools[0]["source"] == "org"
    assert platform_mock.await_count == 0
    assert subscribed_mock.await_count == 0

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_agent_tools_requires_auth(anon_client):
    resp = await anon_client.get("/agent/tools")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_agent_tools_requires_org_id(monkeypatch, anon_client):
    from teardrop.auth import require_auth
    from teardrop.main import app

    async def _mock_auth_no_org() -> dict[str, str]:
        return {
            "sub": "test-user-id",
            "email": "test@example.com",
            "role": "user",
        }

    app.dependency_overrides[require_auth] = _mock_auth_no_org
    try:
        resp = await anon_client.get("/agent/tools")
        assert resp.status_code == 400
        assert "No org_id in token" in resp.json()["detail"]
    finally:
        app.dependency_overrides.pop(require_auth, None)


@pytest.mark.anyio
async def test_agent_tools_pricing_override_applied_for_org_tool(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "false")
    config.get_settings.cache_clear()

    monkeypatch.setattr("teardrop.main.get_tool_pricing_overrides", AsyncMock(return_value={"org/my_tool": 777}))
    monkeypatch.setattr("teardrop.main.get_current_pricing", AsyncMock(return_value=MagicMock(tool_call_cost=1000)))
    monkeypatch.setattr("teardrop.main.list_org_tools", AsyncMock(return_value=[_make_org_tool("my_tool")]))

    resp = await api_client.get("/agent/tools")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    assert len(tools) == 1
    assert tools[0]["cost_usdc"] == 777

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_agent_tools_excludes_inactive_org_tools(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "false")
    config.get_settings.cache_clear()

    monkeypatch.setattr("teardrop.main.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.main.get_current_pricing", AsyncMock(return_value=MagicMock(tool_call_cost=1000)))
    monkeypatch.setattr(
        "teardrop.main.list_org_tools",
        AsyncMock(return_value=[_make_org_tool("active_tool", is_active=True), _make_org_tool("inactive_tool", is_active=False)]),
    )

    resp = await api_client.get("/agent/tools")
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()["tools"]]
    assert "active_tool" in names
    assert "inactive_tool" not in names

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_agent_tools_response_schema_fields(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    config.get_settings.cache_clear()

    monkeypatch.setattr("teardrop.main.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.main.get_current_pricing", AsyncMock(return_value=MagicMock(tool_call_cost=1000)))
    monkeypatch.setattr("teardrop.main.get_marketplace_catalog", AsyncMock(return_value=[_make_platform_tool()]))
    monkeypatch.setattr("teardrop.main.get_subscribed_tools_catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr("teardrop.main.list_org_tools", AsyncMock(return_value=[]))

    resp = await api_client.get("/agent/tools")
    assert resp.status_code == 200
    item = resp.json()["tools"][0]
    assert set(item) == {
        "name",
        "qualified_name",
        "source",
        "access_mode",
        "display_name",
        "description",
        "cost_usdc",
        "input_schema",
    }

    config.get_settings.cache_clear()
