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
