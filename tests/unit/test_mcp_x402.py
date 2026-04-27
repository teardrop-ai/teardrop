# SPDX-License-Identifier: BUSL-1.1
"""Tests for MCP gateway — Phase 3: x402 open-market payment."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import config
from billing import BillingResult


@pytest.fixture
async def x402_client(test_settings, monkeypatch):
    """Client with auth enabled + x402 enabled (no billing for simplicity)."""
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("MCP_X402_ENABLED", "true")
    config.get_settings.cache_clear()
    from app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    config.get_settings.cache_clear()


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
    with (
        patch(
            "billing.build_402_response_body",
            return_value={"error": "Payment required", "accepts": [], "x402Version": 2},
        ),
        patch("billing.build_402_headers", return_value={"X-PAYMENT-REQUIRED": "dGVzdA=="}),
    ):
        resp = await x402_client.post(
            "/tools/mcp",
            content=_tools_call_body(),
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 402
    body = resp.json()
    assert "accepts" in body
    assert "X-PAYMENT-REQUIRED" in resp.headers


@pytest.mark.asyncio
async def test_invalid_payment_returns_402(x402_client):
    """No Bearer + invalid x402 payment → 402 with error."""
    with (
        patch(
            "billing.verify_payment",
            new_callable=AsyncMock,
            return_value=BillingResult(error="Malformed payment"),
        ),
        patch("billing.build_402_headers", return_value={"X-PAYMENT-REQUIRED": "dGVzdA=="}),
    ):
        resp = await x402_client.post(
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
    with patch(
        "billing.verify_payment",
        new_callable=AsyncMock,
        return_value=BillingResult(verified=True, billing_method="x402"),
    ):
        resp = await x402_client.post(
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
    from app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
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
    with patch("billing.verify_payment", new_callable=AsyncMock) as mock_verify:
        resp = await x402_client.post(
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
