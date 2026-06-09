# SPDX-License-Identifier: BUSL-1.1
"""Unit tests for subscribed tool catalog helpers used by GET /agent/tools."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from marketplace import get_subscribed_tools_catalog


@pytest.mark.anyio
async def test_get_subscribed_tools_catalog_empty(monkeypatch):
    mock_pool = MagicMock()
    mock_pool.fetch = AsyncMock(return_value=[])
    monkeypatch.setattr("marketplace._pool", mock_pool)

    tools = await get_subscribed_tools_catalog("org-1")

    assert tools == []


@pytest.mark.anyio
async def test_get_subscribed_tools_catalog_returns_tools(monkeypatch):
    rows = [
        {
            "name": "weather",
            "description": "Weather tool",
            "marketplace_description": "Weather lookup",
            "input_schema": '{"type":"object","properties":{"city":{"type":"string"}}}',
            "base_price_usdc": 5000,
            "org_name": "Acme",
            "org_slug": "acme",
        }
    ]
    mock_pool = MagicMock()
    mock_pool.fetch = AsyncMock(return_value=rows)
    monkeypatch.setattr("marketplace._pool", mock_pool)

    tools = await get_subscribed_tools_catalog("org-1")

    assert len(tools) == 1
    assert tools[0].qualified_name == "acme/weather"
    assert tools[0].name == "weather"
    assert tools[0].display_name == "weather"
    assert tools[0].cost_usdc == 5000
    assert tools[0].author_org_slug == "acme"
    assert tools[0].input_schema["properties"]["city"]["type"] == "string"


@pytest.mark.anyio
async def test_get_subscribed_tools_catalog_override_price(monkeypatch):
    rows = [
        {
            "name": "weather",
            "description": "Weather tool",
            "marketplace_description": "Weather lookup",
            "input_schema": {"type": "object"},
            "base_price_usdc": 5000,
            "org_name": "Acme",
            "org_slug": "acme",
        }
    ]
    mock_pool = MagicMock()
    mock_pool.fetch = AsyncMock(return_value=rows)
    monkeypatch.setattr("marketplace._pool", mock_pool)

    tools = await get_subscribed_tools_catalog(
        "org-1",
        tool_overrides={"acme/weather": 4200, "weather": 4100},
        default_tool_cost=3000,
    )

    assert len(tools) == 1
    assert tools[0].cost_usdc == 4200


@pytest.mark.anyio
async def test_get_subscribed_tools_catalog_default_price_fallback(monkeypatch):
    rows = [
        {
            "name": "weather",
            "description": "Weather tool",
            "marketplace_description": "Weather lookup",
            "input_schema": {"type": "object"},
            "base_price_usdc": 0,
            "org_name": "Acme",
            "org_slug": "acme",
        }
    ]
    mock_pool = MagicMock()
    mock_pool.fetch = AsyncMock(return_value=rows)
    monkeypatch.setattr("marketplace._pool", mock_pool)

    tools = await get_subscribed_tools_catalog(
        "org-1",
        tool_overrides={},
        default_tool_cost=3000,
    )

    assert len(tools) == 1
    assert tools[0].cost_usdc == 3000


@pytest.mark.anyio
async def test_list_agent_tools_org_default_cost_is_zero(monkeypatch):
    from teardrop.routers.agent import list_agent_tools

    org_tool = SimpleNamespace(
        is_active=True,
        name="crm_lookup",
        description="Lookup customer in CRM",
        input_schema={"type": "object", "properties": {"email": {"type": "string"}}},
    )

    monkeypatch.setattr(
        "teardrop.routers.agent.get_settings",
        lambda: SimpleNamespace(marketplace_enabled=False),
    )
    monkeypatch.setattr("teardrop.routers.agent.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "teardrop.routers.agent.get_current_pricing",
        AsyncMock(return_value=SimpleNamespace(tool_call_cost=3000)),
    )
    monkeypatch.setattr("teardrop.routers.agent.list_org_tools", AsyncMock(return_value=[org_tool]))

    response = await list_agent_tools(payload={"org_id": "org-1"})
    body = json.loads(response.body)

    assert len(body["tools"]) == 1
    assert body["tools"][0]["qualified_name"] == "org/crm_lookup"
    assert body["tools"][0]["source"] == "org"
    assert body["tools"][0]["cost_usdc"] == 0
