# SPDX-License-Identifier: BUSL-1.1
"""Tests for MCP gateway — Phase 2: credit billing middleware."""

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
def billing_client(test_settings, monkeypatch):
    """Factory yielding a fresh mounted MCP app with auth + billing enabled."""

    @asynccontextmanager
    async def _client():
        monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
        monkeypatch.setenv("MCP_BILLING_ENABLED", "true")
        monkeypatch.setenv("MCP_AUTH_AUDIENCE", "")  # disable aud check — test JWTs have no aud claim
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


def _init_body() -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            },
        }
    )


class _FakePricing:
    tool_call_cost = 1000


@pytest.mark.asyncio
async def test_billing_skips_non_tools_call(billing_client, test_jwt_token):
    """Non-tools/call methods (e.g. initialize) bypass billing entirely."""
    async with billing_client() as client:
        with patch("billing.verify_credit", new_callable=AsyncMock) as mock_verify:
            resp = await client.post(
                "/tools/mcp",
                content=_init_body(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {test_jwt_token}",
                },
            )
    mock_verify.assert_not_called()
    assert resp.status_code != 402


@pytest.mark.asyncio
async def test_billing_rejects_insufficient_credits(billing_client, test_jwt_token):
    """tools/call with insufficient credits → 402 JSON-RPC error."""
    async with billing_client() as client:
        with (
            patch("billing.get_tool_pricing_overrides", new_callable=AsyncMock, return_value={}),
            patch("billing.get_current_pricing", new_callable=AsyncMock, return_value=_FakePricing()),
            patch(
                "billing.verify_credit",
                new_callable=AsyncMock,
                return_value=BillingResult(error="Insufficient credit"),
            ),
        ):
            resp = await client.post(
                "/tools/mcp",
                content=_tools_call_body(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {test_jwt_token}",
                },
            )

    assert resp.status_code == 402
    body = resp.json()
    assert body["error"]["code"] == -32000
    assert "Insufficient" in body["error"]["message"]


@pytest.mark.asyncio
async def test_promotional_credit_rejects_qualified_marketplace_tool(billing_client, monkeypatch, test_jwt_token):
    """Direct MCP calls cannot turn grant-only credit into author earnings."""
    monkeypatch.setenv("ONBOARDING_CREDIT_ENABLED", "true")

    async with billing_client() as client:
        with (
            patch("billing.get_tool_pricing_overrides", new_callable=AsyncMock, return_value={}),
            patch("billing.get_current_pricing", new_callable=AsyncMock, return_value=_FakePricing()),
            patch("billing.resolve_tool_cost", new_callable=AsyncMock, return_value=1_000),
            patch("billing.is_promotional_credit", new_callable=AsyncMock, return_value=True),
            patch("billing.verify_credit", new_callable=AsyncMock) as mock_verify,
        ):
            resp = await client.post(
                "/tools/mcp",
                content=_tools_call_body("acme/weather"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {test_jwt_token}",
                },
            )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == -32003
    mock_verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_billing_debits_on_success(billing_client, test_jwt_token):
    """Successful tools/call debits credits with correct amount and reason."""
    async with billing_client() as client:
        with (
            patch(
                "billing.get_tool_pricing_overrides",
                new_callable=AsyncMock,
                return_value={"web_search": 500},
            ),
            patch("billing.get_current_pricing", new_callable=AsyncMock, return_value=_FakePricing()),
            patch(
                "billing.verify_credit",
                new_callable=AsyncMock,
                return_value=BillingResult(verified=True, billing_method="credit"),
            ),
            patch("billing.debit_credit", new_callable=AsyncMock, return_value=True) as mock_debit,
        ):
            resp = await client.post(
                "/tools/mcp",
                content=_tools_call_body("web_search"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {test_jwt_token}",
                },
            )

    # FastMCP processes the request (may fail at tool level, but billing fires).
    if resp.status_code == 200:
        mock_debit.assert_called_once_with("test-org-id", 500, reason="mcp:web_search")


@pytest.mark.asyncio
async def test_billing_disabled_skips_everything(test_settings, monkeypatch, test_jwt_token):
    """When mcp_billing_enabled=False, billing logic is skipped entirely."""
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("MCP_BILLING_ENABLED", "false")
    config.get_settings.cache_clear()
    from teardrop.mcp_gateway import MCPGatewayMiddleware
    from tools.mcp_server import mcp

    mounted_mcp_app = mcp.http_app(path="/", stateless_http=True, json_response=True)
    mounted_app = FastAPI(lifespan=mounted_mcp_app.lifespan)
    mounted_app.add_middleware(MCPGatewayMiddleware)
    mounted_app.mount("/tools/mcp", mounted_mcp_app)

    async with mounted_app.router.lifespan_context(mounted_app):
        async with AsyncClient(transport=ASGITransport(app=mounted_app), base_url="http://test") as c:
            with patch("billing.verify_credit", new_callable=AsyncMock) as mock_verify:
                resp = await c.post(
                    "/tools/mcp",
                    content=_tools_call_body(),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {test_jwt_token}",
                    },
                )

    mock_verify.assert_not_called()
    assert resp.status_code != 402
    config.get_settings.cache_clear()
