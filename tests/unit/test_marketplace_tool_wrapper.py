from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
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


@pytest.mark.anyio
async def test_build_marketplace_langchain_tool_supports_mcp_backed_rows(monkeypatch):
    row = {
        "id": "tool-1",
        "org_id": "org-1",
        "name": "tool",
        "description": "desc",
        "marketplace_description": "marketplace desc",
        "mcp_server_id": "srv-1",
        "mcp_tool_name": "remote_tool",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "output_schema": {"type": "object", "properties": {}},
    }

    class _Part:
        def __init__(self, text: str):
            self.text = text

    class _Result:
        def __init__(self, content):
            self.content = content

    class _Session:
        async def call_tool(self, tool_name, kwargs):
            assert tool_name == "remote_tool"
            assert kwargs == {"q": "hi"}
            return _Result([_Part('{"ok": true}')])

    monkeypatch.setattr(
        "mcp_client.crud.get_mcp_server_by_id",
        AsyncMock(return_value=SimpleNamespace(id="srv-1", timeout_seconds=15)),
    )
    monkeypatch.setattr("mcp_client.runtime._get_or_create_session", AsyncMock(return_value=_Session()))
    monkeypatch.setattr("org_tools.runtime._record_event", AsyncMock())

    tool = marketplace._build_marketplace_langchain_tool(row, "acme/tool")
    result = await tool.ainvoke({"q": "hi"})

    assert result == {"ok": True}
