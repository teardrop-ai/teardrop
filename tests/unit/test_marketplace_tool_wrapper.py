from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

import marketplace


@pytest.mark.anyio
async def test_build_marketplace_langchain_tool_rejects_non_get_method():
    row = {
        "id": "tool-1",
        "webhook_url": "https://example.com/hook",
        "webhook_method": "POST",
        "timeout_seconds": 10,
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }
    with pytest.raises(ValueError, match="non-GET"):
        marketplace._build_marketplace_langchain_tool(row, "acme/tool")


@pytest.mark.anyio
async def test_build_subscribed_marketplace_tools_skips_schema_drift(monkeypatch):
    sub = marketplace.MarketplaceSubscription(
        id="sub-1",
        org_id="org-1",
        qualified_tool_name="acme/tool",
        is_active=True,
        subscribed_at=datetime.now(timezone.utc),
        subscribed_schema_hash="oldhash",
    )

    monkeypatch.setattr(marketplace, "get_org_subscriptions", AsyncMock(return_value=[sub]))
    monkeypatch.setattr(
        marketplace,
        "get_marketplace_tool_by_name",
        AsyncMock(
            return_value={
                "id": "tool-1",
                "name": "tool",
                "webhook_url": "https://example.com/hook",
                "webhook_method": "GET",
                "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
                "schema_hash": "newhash",
            }
        ),
    )

    with patch.object(marketplace, "_build_marketplace_langchain_tool") as build_mock:
        tools, by_name = await marketplace.build_subscribed_marketplace_tools("org-1")

    assert tools == []
    assert by_name == {}
    build_mock.assert_not_called()
