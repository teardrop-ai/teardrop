# SPDX-License-Identifier: BUSL-1.1
"""Tests for MCP gateway — Phase 3: x402 open-market payment."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import teardrop.config as config
from billing import BillingResult


@pytest.fixture
def x402_client(test_settings, monkeypatch):
    """Factory yielding a fresh mounted MCP app with x402 enabled."""

    @asynccontextmanager
    async def _client():
        monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
        monkeypatch.setenv("MCP_X402_ENABLED", "true")
        config.get_settings.cache_clear()

        from teardrop.mcp_gateway import MCPGatewayMiddleware
        from tools.mcp_server import mcp

        mounted_mcp_app = mcp.http_app(path="/", stateless_http=True, json_response=True)
        mounted_app = FastAPI(lifespan=mounted_mcp_app.lifespan)
        mounted_app.add_middleware(MCPGatewayMiddleware)
        mounted_app.mount("/tools/mcp", mounted_mcp_app)

        try:
            async with mounted_app.router.lifespan_context(mounted_app):
                async with AsyncClient(transport=ASGITransport(app=mounted_app), base_url="http://test") as client:
                    yield client
        finally:
            config.get_settings.cache_clear()

    return _client


def _tools_call_body(tool_name: str = "web_search", req_id: int = 1) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": req_id,
            "params": {"name": tool_name, "arguments": {"query": "test"}},
        }
    )


@pytest.mark.asyncio
async def test_no_auth_no_payment_returns_402(x402_client):
    """No Bearer + no x402 payment header → 402 with accepts body."""
    seen: dict[str, dict] = {}

    def _body(**kwargs):
        seen["body"] = kwargs
        return {
            "error": kwargs.get("error", "Payment required"),
            "accepts": [],
            "x402Version": 2,
            "resource": kwargs["resource"],
        }

    def _headers(**kwargs):
        seen["headers"] = kwargs
        return {"PAYMENT-REQUIRED": "dGVzdA==", "X-PAYMENT-REQUIRED": "bGVnYWN5"}

    async with x402_client() as client:
        with (
            patch("billing.build_402_response_body", side_effect=_body),
            patch("billing.build_402_headers", side_effect=_headers),
        ):
            resp = await client.post(
                "/tools/mcp",
                content=_tools_call_body(),
                headers={"Content-Type": "application/json"},
            )

    assert resp.status_code == 402
    body = resp.json()
    assert "accepts" in body
    assert body["resource"]["url"] == "http://test/tools/mcp"
    assert "PAYMENT-REQUIRED" in resp.headers
    assert "X-PAYMENT-REQUIRED" in resp.headers
    assert seen["body"]["resource"]["mimeType"] == "application/json"


@pytest.mark.asyncio
async def test_invalid_payment_returns_402(x402_client):
    """No Bearer + invalid x402 payment → 402 with error."""
    async with x402_client() as client:
        with (
            patch(
                "billing.verify_payment",
                new_callable=AsyncMock,
                return_value=BillingResult(error="Malformed payment"),
            ),
            patch(
                "billing.build_402_headers",
                return_value={"PAYMENT-REQUIRED": "dGVzdA==", "X-PAYMENT-REQUIRED": "bGVnYWN5"},
            ),
            patch(
                "billing.build_402_response_body",
                return_value={"error": "Malformed payment", "accepts": [], "x402Version": 2},
            ),
        ):
            resp = await client.post(
                "/tools/mcp",
                content=_tools_call_body(),
                headers={
                    "Content-Type": "application/json",
                    "X-Payment": "bad-payment-data",
                },
            )

    assert resp.status_code == 402
    body = resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_valid_payment_passes_through(x402_client):
    """No Bearer + valid x402 payment → request reaches FastMCP."""
    async with x402_client() as client:
        with patch(
            "billing.verify_payment",
            new_callable=AsyncMock,
            return_value=BillingResult(verified=True, billing_method="x402"),
        ):
            resp = await client.post(
                "/tools/mcp",
                content=_tools_call_body(),
                headers={
                    "Content-Type": "application/json",
                    "X-Payment": "valid-payment-data",
                },
            )

    # Should NOT be 401 or 402 — request passed auth gate.
    assert resp.status_code not in (401, 402)


@pytest.mark.asyncio
async def test_x402_disabled_returns_401(test_settings, monkeypatch):
    """Auth enabled + x402 disabled + no Bearer → 401 (not 402)."""
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("MCP_X402_ENABLED", "false")
    config.get_settings.cache_clear()
    from teardrop.mcp_gateway import MCPGatewayMiddleware
    from tools.mcp_server import mcp

    mounted_mcp_app = mcp.http_app(path="/", stateless_http=True, json_response=True)
    mounted_app = FastAPI(lifespan=mounted_mcp_app.lifespan)
    mounted_app.add_middleware(MCPGatewayMiddleware)
    mounted_app.mount("/tools/mcp", mounted_mcp_app)

    async with mounted_app.router.lifespan_context(mounted_app):
        async with AsyncClient(transport=ASGITransport(app=mounted_app), base_url="http://test") as c:
            resp = await c.post(
                "/tools/mcp",
                content=_tools_call_body(),
                headers={"Content-Type": "application/json"},
            )

    assert resp.status_code == 401
    config.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_bearer_present_skips_x402(x402_client, test_jwt_token):
    """When Bearer token is present, x402 path is never entered."""
    async with x402_client() as client:
        with patch("billing.verify_payment", new_callable=AsyncMock) as mock_verify:
            resp = await client.post(
                "/tools/mcp",
                content=_tools_call_body(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {test_jwt_token}",
                },
            )

    # verify_payment should not be called when Bearer auth succeeds.
    mock_verify.assert_not_called()
    assert resp.status_code != 402
