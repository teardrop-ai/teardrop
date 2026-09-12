# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Tests for the MCP gateway — Phase 1: JWKS endpoint + JWT auth gate."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

import teardrop.config as config  # ── JWKS endpoint ─────────────────────────────────────────────────────────────
from teardrop._meta import APP_VERSION


def test_mcp_bazaar_extension_has_valid_flat_jsonrpc_schema():
    from x402.extensions.bazaar import validate_discovery_extension

    from teardrop.mcp_gateway import _mcp_402_extensions

    bazaar = _mcp_402_extensions()["bazaar"]
    body_schema = bazaar["schema"]["properties"]["input"]["properties"]["body"]

    result = validate_discovery_extension(bazaar)

    assert result.valid, result.errors
    assert bazaar["info"]["input"]["method"] == "POST"
    assert body_schema["properties"]["method"]["const"] == "tools/call"
    assert "$defs" not in json.dumps(body_schema)
    assert "$ref" not in json.dumps(body_schema)


@pytest.mark.asyncio
@pytest.mark.parametrize("payment_header", [None, "invalid-payment"])
async def test_mcp_x402_challenges_include_bazaar_in_body_and_headers(monkeypatch, payment_header):
    import billing
    from teardrop.mcp_gateway import MCPGatewayMiddleware

    headers = [] if payment_header is None else [(b"payment-signature", payment_header.encode())]
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("test", 443),
            "path": "/tools/mcp",
            "headers": headers,
        }
    )
    seen: dict[str, dict] = {}

    def _body(**kwargs):
        seen["body"] = kwargs
        return {
            "error": kwargs.get("error", "Payment required"),
            "accepts": [],
            "x402Version": 2,
            "resource": kwargs["resource"],
            "extensions": kwargs["extensions"],
        }

    def _headers(**kwargs):
        seen["headers"] = kwargs
        return {"PAYMENT-REQUIRED": "encoded", "X-PAYMENT-REQUIRED": "legacy"}

    monkeypatch.setattr(billing, "build_402_response_body", _body)
    monkeypatch.setattr(billing, "build_402_headers", _headers)
    verify_mock = AsyncMock(return_value=SimpleNamespace(verified=False, error="Invalid payment"))
    monkeypatch.setattr(billing, "verify_payment", verify_mock)

    response = await MCPGatewayMiddleware(FastAPI())._handle_x402_auth(request)

    assert response is not None
    assert response.status_code == 402
    body = json.loads(response.body)
    assert body["extensions"]["bazaar"]["info"]["input"]["method"] == "POST"
    assert body["resource"]["url"] == "https://test/tools/mcp"
    assert seen["headers"]["extensions"] == seen["body"]["extensions"]
    assert "org_id" not in json.dumps(body)
    if payment_header is None:
        verify_mock.assert_not_awaited()
        assert body["error"] == "Payment required"
    else:
        verify_mock.assert_awaited_once_with(payment_header)
        assert body["error"] == "Invalid payment"


@pytest.mark.asyncio
async def test_jwks_returns_valid_key(test_settings):
    """GET /.well-known/jwks.json returns a valid RSA JWK."""
    from teardrop.main import app

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
    from teardrop.main import app

    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        yield c
    config.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_auth_gate_rejects_missing_token(mcp_client):
    """POST /tools/mcp without Authorization header executing tools/call → 401."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


@pytest.mark.asyncio
async def test_auth_gate_rejects_invalid_token(mcp_client):
    """POST /tools/mcp with garbage Bearer executing tools/call → 401."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer not-a-valid-jwt",
        },
    )
    assert resp.status_code == 401
    assert "invalid_token" in resp.headers.get("WWW-Authenticate", "")


@pytest.mark.asyncio
async def test_auth_gate_rejects_expired_token(mcp_client, test_settings):
    """POST /tools/mcp with an expired JWT executing tools/call → 401."""
    from datetime import datetime, timedelta, timezone

    import jwt as pyjwt

    payload = {
        "sub": "test-user",
        "iss": test_settings.jwt_issuer,
        "iat": datetime.now(timezone.utc) - timedelta(hours=2),
        "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        "org_id": "test-org",
    }
    expired_token = pyjwt.encode(payload, test_settings.jwt_private_key, algorithm="RS256")

    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {expired_token}",
        },
    )
    assert resp.status_code == 401
    assert "token_expired" in resp.headers.get("WWW-Authenticate", "")


@pytest.mark.asyncio
async def test_auth_gate_passes_valid_token(mcp_client, test_jwt_token):
    """POST /tools/mcp with valid JWT executing tools/call passes through to MCPServer."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {test_jwt_token}",
        },
    )
    # MCPServer handles the request — may be 200 or a JSON-RPC error, but NOT 401.
    assert resp.status_code != 401


@pytest.mark.asyncio
async def test_auth_gate_disabled_passes_through(test_settings, monkeypatch):
    """When mcp_auth_enabled=False, unauthenticated tools/call passes through."""
    monkeypatch.setenv("MCP_AUTH_ENABLED", "false")
    config.get_settings.cache_clear()
    from teardrop.main import app

    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        resp = await c.post(
            "/tools/mcp",
            content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
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


@pytest.mark.asyncio
async def test_mcp_app_real_handshake():
    """Verify MCPServer stateless streamable layer returns a valid initialize handshake."""
    from teardrop.app import mcp_app

    async with mcp_app.router.lifespan_context(mcp_app):
        # We test MCPServer's ASGI app directly here because testing it through proxy
        # requires the full FastAPI DB-connected lifespan.
        async with AsyncClient(
            transport=ASGITransport(app=mcp_app), base_url="http://test", headers={"Accept": "application/json"}
        ) as c:
            resp = await c.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "result" in data
            assert data["result"]["protocolVersion"] == "2024-11-05"
            assert data["result"]["serverInfo"]["name"] == "Teardrop"
            assert data["result"]["serverInfo"]["version"] == APP_VERSION

            # Check tools/list is also available
            resp_list = await c.post(
                "/",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            assert resp_list.status_code == 200
            list_data = resp_list.json()
            assert "tools" in list_data["result"]
            assert len(list_data["result"]["tools"]) > 0
            calculate_tool = next(tool for tool in list_data["result"]["tools"] if tool["name"] == "calculate")
            assert "outputSchema" in calculate_tool
            assert "description" in calculate_tool["inputSchema"]["properties"]["expression"]


@pytest.mark.asyncio
async def test_mounted_mcp_normalizes_no_slash_path():
    """POST /tools/mcp should hit the mounted MCP app, not /tools/{tool_id}."""
    from teardrop.mcp_gateway import MCPGatewayMiddleware
    from tools.mcp_server import build_mcp_app, create_mcp_server

    mcp = create_mcp_server()
    mounted_mcp_app = build_mcp_app(mcp)
    mounted_app = FastAPI(lifespan=lambda _: mcp.session_manager.run())
    mounted_app.add_middleware(MCPGatewayMiddleware)
    mounted_app.mount("/tools/mcp", mounted_mcp_app)

    async with mounted_app.router.lifespan_context(mounted_app):
        async with AsyncClient(
            transport=ASGITransport(app=mounted_app),
            base_url="http://test",
            headers={"Accept": "application/json"},
        ) as c:
            resp = await c.post(
                "/tools/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["serverInfo"]["name"] == "Teardrop"


@pytest.mark.asyncio
async def test_public_discovery_bypass_success(mcp_client):
    """POST /tools/mcp with initialize method bypasses auth for all clients."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1}),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "RandomClient/1.0",
        },
    )
    # Bypasses 401/402 gate completely and should not fall through to 405.
    # ASGITransport still skips the full app lifespan, so a 500 is acceptable here.
    assert resp.status_code not in (401, 402, 405)


@pytest.mark.asyncio
async def test_smithery_events_list_returns_empty_catalog(mcp_client):
    """Smithery trigger discovery should return an empty event catalog, not a validation error."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "ai.smithery/events/list", "id": 3}),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "SmitheryBot/1.0 (+https://smithery.ai)",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"jsonrpc": "2.0", "id": 3, "result": {"events": []}}


@pytest.mark.asyncio
async def test_execution_blocked_for_all(mcp_client):
    """POST /tools/mcp requesting tools/call is blocked without auth."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "SmitheryBot/1.0 (+https://smithery.ai)",
        },
    )
    # Execution is not bypassed, so it gets blocked by Phase 1 auth (401)
    assert resp.status_code == 401
