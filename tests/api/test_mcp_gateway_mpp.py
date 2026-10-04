# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""API tests for MPP charge on the /tools/mcp gateway (x402-first PaymentScheme seam)."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from billing import BillingResult
from billing.mpp import MppVerifyOutcome, build_mpp_challenge


@pytest.fixture
def _stub_402_body():
    with (
        patch(
            "billing.build_402_response_body",
            side_effect=lambda **kw: {"x402Version": 2, "accepts": [], **{k: v for k, v in kw.items() if v is not None}},
        ),
        # Header construction falls back to the requirements cache that only
        # init_billing() populates; keep the challenge tests hermetic.
        patch("billing.build_402_headers", new=MagicMock(return_value={"PAYMENT-REQUIRED": "x402"})),
    ):
        yield


_RECIPIENT = "0x" + "ab" * 20
_CURRENCY = "0x" + "cd" * 20
_PAYER = "0x" + "42" * 20
_TX = "0x" + "11" * 32
_TOOL_COST = 10_000


def _tool_call(credential=None) -> dict:
    meta = {} if credential is None else {"org.paymentauth/credential": credential}
    return {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {"name": "calculate", "arguments": {"expression": "1+1"}, "_meta": meta},
    }


def _enable_mpp(test_settings, *, billing: bool = True) -> None:
    test_settings.mcp_auth_enabled = True
    test_settings.mcp_x402_enabled = True
    test_settings.mcp_billing_enabled = billing
    test_settings.mpp_enabled = True
    test_settings.mpp_recipient = _RECIPIENT
    test_settings.mpp_currency = _CURRENCY
    test_settings.mpp_secret_key = "k" * 32
    test_settings.base_rpc_url = "http://rpc.test"
    test_settings.x402_network = "eip155:8453"


def _credential(settings) -> dict:
    return {
        "challenge": build_mpp_challenge(tool_cost_usdc=_TOOL_COST, settings=settings),
        "source": _PAYER,
        "payload": {"type": "hash", "hash": _TX},
    }


def _fixed_price():
    return patch(
        "teardrop.mcp_gateway.MCPGatewayMiddleware._resolve_tool_cost",
        new=AsyncMock(return_value=_TOOL_COST),
    )


def _receipt() -> dict:
    def word(address: str) -> str:
        return "0x" + address[2:].rjust(64, "0")

    return {
        "status": "0x1",
        "blockNumber": "0x10",
        "logs": [
            {
                "address": _CURRENCY,
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    word(_PAYER),
                    word(_RECIPIENT),
                ],
                "data": hex(_TOOL_COST),
            }
        ],
    }


def _chain_ok():
    now = int(datetime.now(timezone.utc).timestamp())
    return (
        patch("billing.mpp._eth_get_receipt", new=AsyncMock(return_value=_receipt())),
        patch("billing.mpp._eth_block_timestamp", new=AsyncMock(return_value=now)),
        patch("billing.claim_payment_nonce", new=AsyncMock(return_value=True)),
    )


async def _post_executing_call(body: dict, headers: dict | None = None):
    """POST through a gateway-mounted MCP app whose session manager runs.

    ``api_client`` never runs lifespan, so the MCP session task group is
    unavailable there — execution tests must use this harness.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from teardrop.mcp_gateway import MCPGatewayMiddleware
    from tools.mcp_server import build_mcp_app, create_mcp_server

    mcp = create_mcp_server()
    app = FastAPI(lifespan=lambda _: mcp.session_manager.run())
    app.add_middleware(MCPGatewayMiddleware)
    app.mount("/tools/mcp", build_mcp_app(mcp))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/tools/mcp", json=body, headers={"Accept": "application/json", **(headers or {})})


@pytest.mark.anyio
async def test_mpp_disabled_leaves_x402_challenge_untouched(api_client, test_settings, _stub_402_body):
    _enable_mpp(test_settings)
    test_settings.mpp_enabled = False
    with _fixed_price():
        resp = await api_client.post("/tools/mcp", json=_tool_call({"bad": True}))
    assert resp.status_code == 402, resp.text
    assert "error" not in resp.json()
    assert "www-authenticate" not in resp.headers


@pytest.mark.anyio
async def test_http_no_payment_keeps_x402_and_advertises_mpp_header(api_client, test_settings, _stub_402_body):
    """x402-first: body and PAYMENT-REQUIRED are unchanged; MPP rides WWW-Authenticate."""
    _enable_mpp(test_settings)
    with _fixed_price():
        resp = await api_client.post("/tools/mcp", json=_tool_call())
    assert resp.status_code == 402, resp.text
    body = resp.json()
    assert body["x402Version"] == 2 and "error" not in body
    assert resp.headers["payment-required"] == "x402"
    www = resp.headers["www-authenticate"]
    assert www.startswith("Payment ") and 'method="evm"' in www and 'intent="charge"' in www


@pytest.mark.anyio
async def test_mcp_no_payment_keeps_x402_result_and_offers_mpp_in_meta(api_client, test_settings, _stub_402_body):
    _enable_mpp(test_settings)
    with _fixed_price():
        resp = await api_client.post("/tools/mcp", json=_tool_call(), headers={"Accept": "application/json, text/event-stream"})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["isError"] is True and result["structuredContent"]["x402Version"] == 2
    offer = result["_meta"]["org.paymentauth/challenges"][0]
    assert offer["method"] == "evm" and offer["request"]["amount"] == str(_TOOL_COST)
    assert offer["request"]["methodDetails"]["chainId"] == 8453


@pytest.mark.anyio
async def test_malformed_credential_returns_32602_with_replacement(api_client, test_settings):
    _enable_mpp(test_settings)
    with _fixed_price():
        resp = await api_client.post("/tools/mcp", json=_tool_call({"bad": True}))
    assert resp.status_code == 402, resp.text
    error = resp.json()["error"]
    assert error["code"] == -32602
    assert error["data"]["challenges"][0]["method"] == "evm"
    assert resp.headers["www-authenticate"].startswith("Payment ")


@pytest.mark.anyio
async def test_billing_disabled_never_accepts_mpp(api_client, test_settings, _stub_402_body):
    """An inactive billing gate would strand the payer's funds, so MPP stays off entirely."""
    _enable_mpp(test_settings, billing=False)
    with _fixed_price(), patch("billing.mpp.verify_mpp_payment", new=AsyncMock()) as verify_mpp:
        resp = await api_client.post("/tools/mcp", json=_tool_call(_credential(test_settings)))
    verify_mpp.assert_not_awaited()
    assert resp.status_code == 402 and "error" not in resp.json()


@pytest.mark.anyio
async def test_valid_credential_executes_records_and_attaches_receipt(test_settings):
    _enable_mpp(test_settings)
    p_receipt, p_block, p_claim = _chain_ok()
    with (
        _fixed_price(),
        p_receipt,
        p_block,
        p_claim,
        patch("billing.verify_payment", new=AsyncMock()) as verify_x402,
        patch("billing.reserve_payer_spend", new=AsyncMock(return_value=False)) as reserve,
        patch("teardrop.rate_limit.check_auth_lockout", new=AsyncMock(return_value=(True, 300))) as lockout,
        patch("billing.settle_payment", new=AsyncMock()) as settle,
        patch("teardrop.usage.record_mcp_call_event", new=AsyncMock()) as record_event,
        patch("marketplace.record_marketplace_tool_usage_many", new=AsyncMock()),
    ):
        resp = await _post_executing_call(_tool_call(_credential(test_settings)))

    assert resp.status_code == 200, resp.text
    receipt = resp.json()["result"]["_meta"]["org.paymentauth/receipt"]
    assert (receipt["status"], receipt["method"], receipt["reference"]) == ("success", "evm", _TX)
    header = resp.headers["payment-receipt"]
    assert json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))["reference"] == _TX
    # Prepaid: no x402 verification/settlement, and post-payment refusals never run.
    verify_x402.assert_not_awaited()
    settle.assert_not_awaited()
    reserve.assert_not_awaited()
    lockout.assert_not_awaited()
    args = record_event.await_args.args
    assert (args[2], args[4], args[5], args[6], args[7]) == (_PAYER, "mpp", _TOOL_COST, "settled", _TX)


@pytest.mark.anyio
async def test_http_authorization_payment_credential_executes(test_settings):
    _enable_mpp(test_settings)
    token = base64.urlsafe_b64encode(json.dumps(_credential(test_settings)).encode()).rstrip(b"=").decode()
    p_receipt, p_block, p_claim = _chain_ok()
    with (
        _fixed_price(),
        p_receipt,
        p_block,
        p_claim,
        patch("teardrop.usage.record_mcp_call_event", new=AsyncMock()),
        patch("marketplace.record_marketplace_tool_usage_many", new=AsyncMock()),
    ):
        resp = await _post_executing_call(_tool_call(), headers={"Authorization": f"Payment {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["_meta"]["org.paymentauth/receipt"]["reference"] == _TX


@pytest.mark.anyio
async def test_failed_execution_after_payment_is_still_ledgered(test_settings):
    """The transfer is final; a failed tool must not leave received funds off the ledger."""
    _enable_mpp(test_settings)
    outcome = MppVerifyOutcome("ok", tx_hash=_TX, source=_PAYER, challenge_id="ch-1")
    with (
        _fixed_price(),
        patch("billing.mpp.verify_mpp_payment", new=AsyncMock(return_value=outcome)),
        patch(
            "teardrop.mcp_gateway.MCPGatewayMiddleware._response_indicates_failure",
            new=AsyncMock(return_value=True),
        ),
        patch("teardrop.usage.record_mcp_call_event", new=AsyncMock()) as record_event,
        patch("teardrop.rate_limit.record_auth_failure", new=AsyncMock()) as record_failure,
    ):
        resp = await _post_executing_call(_tool_call(_credential(test_settings)))
    assert resp.status_code == 200, resp.text
    assert resp.headers["payment-receipt"]
    assert record_event.await_args.args[4:7] == ("mpp", _TOOL_COST, "settled")
    record_failure.assert_not_awaited()


@pytest.mark.anyio
async def test_unpaid_x402_call_still_hits_ip_failure_budget(api_client, test_settings):
    _enable_mpp(test_settings)
    with _fixed_price(), patch("teardrop.rate_limit.check_auth_lockout", new=AsyncMock(return_value=(True, 120))):
        resp = await api_client.post("/tools/mcp", json=_tool_call())
    assert resp.status_code == 429
    assert resp.headers["x-ratelimit-scope"] == "failure-budget"


@pytest.mark.anyio
async def test_mpp_success_never_reaches_x402_verification(test_settings):
    _enable_mpp(test_settings)
    outcome = MppVerifyOutcome("ok", tx_hash=_TX, source=_PAYER, challenge_id="ch-1")
    with (
        _fixed_price(),
        patch("billing.mpp.verify_mpp_payment", new=AsyncMock(return_value=outcome)) as verify_mpp,
        patch(
            "billing.verify_payment",
            new=AsyncMock(return_value=BillingResult(error="Malformed payment header")),
        ) as verify_x402,
        patch("teardrop.usage.record_mcp_call_event", new=AsyncMock()),
        patch("marketplace.record_marketplace_tool_usage_many", new=AsyncMock()),
    ):
        resp = await _post_executing_call(_tool_call(_credential(test_settings)))
    assert resp.status_code == 200, resp.text
    verify_mpp.assert_awaited_once()
    verify_x402.assert_not_awaited()
