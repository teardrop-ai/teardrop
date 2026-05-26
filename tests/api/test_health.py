"""API tests for / and /health endpoints."""

from __future__ import annotations

import pytest


@pytest.mark.anyio
async def test_health_ok(api_client):
    resp = await api_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "teardrop"
    assert "version" in body
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["permissions-policy"] == "geolocation=(), microphone=()"


@pytest.mark.anyio
async def test_root_redirects_to_docs(api_client):
    resp = await api_client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert "/docs" in resp.headers.get("location", "")


@pytest.mark.anyio
async def test_agent_card_shape(api_client):
    resp = await api_client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Teardrop"
    assert "skills" in body
    assert "tools" in body
    assert "authentication" in body
    assert body["endpoints"]["mcp_tools"] == "/tools/mcp"
    assert body["capabilities"]["billing"]["pricing_endpoint"] == "/billing/pricing"


@pytest.mark.anyio
async def test_agent_card_marketplace_discovery(api_client, test_settings):
    test_settings.marketplace_enabled = True

    resp = await api_client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    body = resp.json()
    assert body["capabilities"]["marketplace"] == {
        "enabled": True,
        "catalog_endpoint": "/marketplace/catalog",
        "mcp_gateway_endpoint": "/tools/mcp",
    }
    assert body["endpoints"]["marketplace_catalog"] == "/marketplace/catalog"


@pytest.mark.anyio
async def test_agent_card_omits_marketplace_when_disabled(api_client, test_settings):
    test_settings.marketplace_enabled = False

    resp = await api_client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    body = resp.json()
    assert "marketplace" not in body["capabilities"]
    assert "marketplace_catalog" not in body["endpoints"]
