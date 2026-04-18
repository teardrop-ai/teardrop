# SPDX-License-Identifier: BUSL-1.1
"""Tests for the MCP gateway — Phase 1: JWKS endpoint + JWT auth gate."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

import config

# ── JWKS endpoint ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_jwks_returns_valid_key(test_settings):
    """GET /.well-known/jwks.json returns a valid RSA JWK."""
    from app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/.well-known/jwks.json")

    assert resp.status_code == 200
    body = resp.json()
    assert "keys" in body
    assert len(body["keys"]) == 1

    key = body["keys"][0]
    assert key["kty"] == "RSA"
    assert key["alg"] == "RS256"
    assert key["use"] == "sig"
    assert key["kid"] == "teardrop-rs256"
    assert len(key["n"]) > 10  # non-trivial modulus
    assert key["e"]  # non-empty exponent


# ── Auth gate ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def mcp_client(test_settings, monkeypatch):
    """AsyncClient with mcp_auth_enabled=True and no dep overrides."""
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    # Disable audience check so test tokens (which have no 'aud' claim) pass through.
    monkeypatch.setenv("MCP_AUTH_AUDIENCE", "")
    config.get_settings.cache_clear()
    from app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    config.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_auth_gate_rejects_missing_token(mcp_client):
    """POST /tools/mcp without Authorization header → 401."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


@pytest.mark.asyncio
async def test_auth_gate_rejects_invalid_token(mcp_client):
    """POST /tools/mcp with garbage Bearer → 401."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1}),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer not-a-valid-jwt",
        },
    )
    assert resp.status_code == 401
    assert "invalid_token" in resp.headers.get("WWW-Authenticate", "")


@pytest.mark.asyncio
async def test_auth_gate_rejects_expired_token(mcp_client, test_settings):
    """POST /tools/mcp with an expired JWT → 401."""
    from datetime import datetime, timedelta, timezone

    import jwt as pyjwt

    payload = {
        "sub": "test-user",
        "iss": test_settings.jwt_issuer,
        "iat": datetime.now(timezone.utc) - timedelta(hours=2),
        "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        "org_id": "test-org",
    }
    expired_token = pyjwt.encode(
        payload, test_settings.jwt_private_key, algorithm="RS256"
    )

    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {expired_token}",
        },
    )
    assert resp.status_code == 401
    assert "token_expired" in resp.headers.get("WWW-Authenticate", "")


@pytest.mark.asyncio
async def test_auth_gate_passes_valid_token(mcp_client, test_jwt_token):
    """POST /tools/mcp with valid JWT passes through to FastMCP layer."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {test_jwt_token}",
        },
    )
    # FastMCP handles the request — may be 200 or a JSON-RPC error, but NOT 401.
    assert resp.status_code != 401


@pytest.mark.asyncio
async def test_auth_gate_disabled_passes_through(test_settings, monkeypatch):
    """When mcp_auth_enabled=False, unauthenticated requests pass through."""
    monkeypatch.setenv("MCP_AUTH_ENABLED", "false")
    config.get_settings.cache_clear()
    from app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/tools/mcp",
            content=json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1}),
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code != 401
    config.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_non_mcp_path_not_intercepted(mcp_client):
    """Middleware does not affect non-MCP paths like /health."""
    resp = await mcp_client.get("/health")
    # /health should work without auth regardless of mcp_auth_enabled.
    assert resp.status_code in (200, 503)  # ok or degraded (no DB in tests)
