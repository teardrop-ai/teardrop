"""Regression coverage for onboarding credit in the marketplace MCP endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.anyio
async def test_promotional_credit_cannot_execute_marketplace_tool(api_client, test_settings, monkeypatch):
    """The JSON-RPC marketplace route must reject before executing or debiting."""
    monkeypatch.setattr(test_settings, "marketplace_enabled", True)
    monkeypatch.setattr(test_settings, "billing_enabled", True)
    monkeypatch.setattr(test_settings, "onboarding_credit_enabled", True)
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_settings", lambda: test_settings)
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.check_org_subscription", AsyncMock(return_value=True))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.is_promotional_credit", AsyncMock(return_value=True))
    execute_mock = AsyncMock()
    debit_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace_mcp._execute_marketplace_tool", execute_mock)
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.debit_credit", debit_mock)

    response = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "acme/weather", "arguments": {}},
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == -32003
    execute_mock.assert_not_awaited()
    debit_mock.assert_not_awaited()
