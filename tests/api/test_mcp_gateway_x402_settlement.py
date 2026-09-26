# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""End-to-end x402 settlement through the production-style mounted MCP gateway."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import teardrop.config as config


def _tools_call() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "calculate", "arguments": {"expression": "1+1"}},
    }


@pytest.fixture
def x402_gateway_env(test_settings, monkeypatch):
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("MCP_BILLING_ENABLED", "true")
    monkeypatch.setenv("MCP_X402_ENABLED", "true")
    monkeypatch.setenv("MARKETPLACE_ENABLED", "false")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _patch_billing(monkeypatch, *, tool_cost: int = 2_000):
    import billing

    scoped = [SimpleNamespace(scheme="exact", network="eip155:8453", amount=str(tool_cost))]
    verified = billing.BillingResult(
        verified=True,
        payer="0x" + "a" * 40,
        scheme="exact",
        payment_requirements=scoped[0],
    )
    settled = billing.BillingResult(
        verified=True,
        settled=True,
        tx_hash="0xtx",
        payer=verified.payer,
        amount_usdc=tool_cost,
        payment_requirements=verified.payment_requirements,
    )
    mocks = SimpleNamespace(
        scoped=scoped,
        build=lambda amount: scoped if amount == tool_cost else [],
        verify=AsyncMock(return_value=verified),
        settle=AsyncMock(return_value=settled),
        release=AsyncMock(),
        record=AsyncMock(),
        body=None,
    )
    original_body = billing.build_402_response_body

    def _body(**kwargs):
        mocks.body = kwargs
        return original_body(**{**kwargs, "requirements": []})

    monkeypatch.setattr(billing, "build_exact_payment_requirements", mocks.build)
    monkeypatch.setattr(billing, "build_402_response_body", _body)
    monkeypatch.setattr(billing, "build_402_headers", lambda **kwargs: {})
    monkeypatch.setattr(billing, "verify_payment", mocks.verify)
    monkeypatch.setattr(billing, "settle_payment", mocks.settle)
    monkeypatch.setattr(billing, "release_payment_nonce", mocks.release)
    monkeypatch.setattr(billing, "reserve_payer_spend", AsyncMock(return_value=True))
    monkeypatch.setattr(billing, "get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr(billing, "get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr(billing, "resolve_tool_cost", AsyncMock(return_value=tool_cost))
    monkeypatch.setattr("teardrop.usage.record_mcp_call_event", mocks.record)
    monkeypatch.setattr("marketplace.record_marketplace_tool_usage_many", AsyncMock())
    return mocks


async def _post_paid_call(headers: dict[str, str]):
    from teardrop.mcp_gateway import MCPGatewayMiddleware, MCPPathNormalizer
    from tools.mcp_server import build_mcp_app, create_mcp_server

    mcp = create_mcp_server()
    app = FastAPI(lifespan=lambda _: mcp.session_manager.run())
    app.add_middleware(MCPPathNormalizer)
    app.mount("/tools/mcp", MCPGatewayMiddleware(build_mcp_app(mcp), mounted=True))

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/tools/mcp", json=_tools_call(), headers=headers)


@pytest.mark.asyncio
async def test_challenge_is_priced_at_tool_cost(x402_gateway_env, monkeypatch):
    mocks = _patch_billing(monkeypatch, tool_cost=2_000)

    response = await _post_paid_call({"Accept": "application/json"})

    assert response.status_code == 402
    assert mocks.body["requirements"] is mocks.scoped


@pytest.mark.asyncio
async def test_verified_header_payment_settles_through_mounted_gateway(x402_gateway_env, monkeypatch):
    mocks = _patch_billing(monkeypatch)

    response = await _post_paid_call({"Accept": "application/json", "X-PAYMENT": "signed-payment"})

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    mocks.verify.assert_awaited_once_with("signed-payment", mocks.scoped)
    mocks.settle.assert_awaited_once()
    assert mocks.settle.await_args.kwargs["actual_cost_usdc"] == 2_000
    mocks.record.assert_awaited_once()
    assert mocks.record.await_args.args[5:7] == (2_000, "settled")


@pytest.mark.asyncio
async def test_verified_payment_is_released_when_billing_disabled(x402_gateway_env, monkeypatch):
    monkeypatch.setenv("MCP_BILLING_ENABLED", "false")
    config.get_settings.cache_clear()
    mocks = _patch_billing(monkeypatch)

    response = await _post_paid_call({"Accept": "application/json", "X-PAYMENT": "signed-payment"})

    assert response.status_code == 503
    assert "error" in json.loads(response.text)
    mocks.settle.assert_not_awaited()
    mocks.release.assert_awaited_once_with("signed-payment")
