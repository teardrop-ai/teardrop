# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for billing protection — failed tool calls must not debit credit.

Covers the two settle paths:
    - app.py mcp_jsonrpc_handler debit gate (``execution_failed`` check).
    - mcp_gateway.MCPGateway._settle_billing skips when execution_failed=True.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from billing import BillingResult
from teardrop.mcp_gateway import MCPGatewayMiddleware


@pytest.mark.asyncio
async def test_record_mcp_outcome_schedules_sanitized_x402_event():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state.mcp_call_event_id = "server-call-1"
    request.state.x402_billing = BillingResult(payer="0xabc", payment_payload="secret-payload")

    with patch("teardrop.usage.record_mcp_call_event", new_callable=AsyncMock) as record_mock:
        gateway._record_mcp_outcome(request, None, "platform/get_price", 200, "x402", "settled", "0xtx")
        await asyncio.sleep(0)

    record_mock.assert_awaited_once_with(
        "server-call-1",
        "",
        "0xabc",
        "platform/get_price",
        "x402",
        200,
        "settled",
        "0xtx",
    )


@pytest.mark.asyncio
async def test_settle_billing_skips_debit_on_failed_execution():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock(spec=[])  # no x402_billing attr
    response = MagicMock()
    pending = ("org-1", 100, "test_tool", "req-1")

    with (
        patch("billing.debit_credit", new_callable=AsyncMock) as debit_mock,
        patch("billing.settle_payment", new_callable=AsyncMock) as settle_mock,
        patch.object(gateway, "_record_mcp_outcome") as record_mock,
    ):
        result = await gateway._settle_billing(request, pending, response, execution_failed=True)

    # Neither debit nor settle should fire.
    debit_mock.assert_not_called()
    settle_mock.assert_not_called()
    record_mock.assert_not_called()
    assert result is response


@pytest.mark.asyncio
async def test_settle_billing_releases_x402_reservation_on_failed_execution():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.headers = {"x-payment": "signed-payment"}
    request.state = SimpleNamespace(
        x402_billing=BillingResult(payer="0xabc"),
        mcp_x402_reserved=True,
    )
    response = MagicMock()

    with patch("billing.release_payment_nonce", new=AsyncMock()) as release_mock:
        result = await gateway._settle_billing(
            request,
            (None, 100, "test_tool", "req-1"),
            response,
            execution_failed=True,
        )

    release_mock.assert_awaited_once_with("signed-payment")
    assert request.state.mcp_x402_reserved is False
    assert result is response


@pytest.mark.asyncio
async def test_settle_billing_debits_on_success():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock(spec=[])  # no x402_billing
    response = MagicMock()
    pending = ("org-1", 100, "test_tool", "req-1")  # no slash → skip earnings branch

    with (
        patch("billing.debit_credit", new_callable=AsyncMock, return_value=(True, 100)) as debit_mock,
        patch.object(gateway, "_record_mcp_outcome") as record_mock,
    ):
        result = await gateway._settle_billing(request, pending, response, execution_failed=False)

    debit_mock.assert_called_once()
    record_mock.assert_called_once_with(request, "org-1", "test_tool", 100, "credit", "settled")
    assert result is response


@pytest.mark.asyncio
async def test_settle_billing_x402_rejected_skips_earnings():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock()
    request.state.x402_billing = MagicMock()
    response = MagicMock()
    pending = ("org-1", 100, "acme/test_tool", "req-1")

    with (
        patch(
            "billing.settle_payment",
            new=AsyncMock(return_value=BillingResult(verified=True, settled=False, error="rejected")),
        ) as settle_mock,
        patch("marketplace.get_marketplace_tool_by_name", new=AsyncMock()) as get_tool_mock,
        patch.object(gateway, "_record_mcp_outcome") as record_mock,
    ):
        result = await gateway._settle_billing(request, pending, response, execution_failed=False)

    settle_mock.assert_awaited_once()
    get_tool_mock.assert_not_called()
    record_mock.assert_called_once_with(request, "org-1", "acme/test_tool", 100, "x402", "failed")
    assert result is response


@pytest.mark.asyncio
async def test_settle_billing_x402_success_records_earnings():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock()
    request.state.x402_billing = MagicMock()
    response = MagicMock()
    pending = ("org-1", 100, "acme/test_tool", "req-1")

    with (
        patch(
            "billing.settle_payment",
            new=AsyncMock(return_value=BillingResult(verified=True, settled=True, tx_hash="0xabc")),
        ) as settle_mock,
        patch("marketplace.get_marketplace_tool_by_name", new=AsyncMock(return_value={"org_id": "author-org"})),
        patch("marketplace.record_tool_call_earnings", new=AsyncMock()),
        patch("teardrop.mcp_gateway.asyncio.create_task") as create_task_mock,
        patch.object(gateway, "_record_mcp_outcome") as record_mock,
    ):
        result = await gateway._settle_billing(request, pending, response, execution_failed=False)

    settle_mock.assert_awaited_once()
    assert create_task_mock.call_count == 2
    record_mock.assert_called_once_with(request, "org-1", "acme/test_tool", 100, "x402", "settled", "0xabc")
    assert result is response


@pytest.mark.asyncio
async def test_response_indicates_failure_detects_iserror_true():
    """Body iterator with isError=true triggers skip path."""
    body = b'{"jsonrpc":"2.0","id":1,"result":{"isError":true,"content":[]}}'

    response = MagicMock()

    async def _iter():
        yield body

    response.body_iterator = _iter()
    failed = await MCPGatewayMiddleware._response_indicates_failure(response)
    assert failed is True


@pytest.mark.asyncio
async def test_response_indicates_failure_no_error_returns_false():
    body = b'{"jsonrpc":"2.0","id":1,"result":{"isError":false,"content":[{"type":"text","text":"ok"}]}}'
    response = MagicMock()

    async def _iter():
        yield body

    response.body_iterator = _iter()
    failed = await MCPGatewayMiddleware._response_indicates_failure(response)
    assert failed is False


@pytest.mark.asyncio
async def test_response_indicates_failure_handles_unparseable_body():
    """Unparseable bodies default to ``billing proceeds`` (returns False)."""
    response = MagicMock()

    async def _iter():
        yield b"not-json"

    response.body_iterator = _iter()
    failed = await MCPGatewayMiddleware._response_indicates_failure(response)
    assert failed is False


# ─── _billing_gate: x402 callers must produce a pending settlement tuple ──────


def _gate_request(body: bytes, *, is_x402: bool, org_id):
    request = MagicMock()
    request.method = "POST"
    request.headers = {"x-payment": "signed-payment"}
    request.body = AsyncMock(return_value=body)
    request.state = SimpleNamespace(
        mcp_org_id=org_id,
        x402_billing=BillingResult(payer="0xabc") if is_x402 else None,
    )
    return request


@pytest.mark.asyncio
async def test_billing_gate_x402_returns_pending_tuple():
    """x402 tools/call must return a pending tuple (org_id=None) so settlement runs.

    Regression: the gate previously short-circuited to None for x402, which meant
    the post-response settlement hook never fired and callers were never charged.
    """
    gateway = MCPGatewayMiddleware(app=MagicMock())
    body = b'{"jsonrpc":"2.0","id":"req-1","method":"tools/call","params":{"name":"get_price"}}'
    request = _gate_request(body, is_x402=True, org_id=None)

    settings = MagicMock()
    settings.mcp_billing_enabled = True
    settings.marketplace_enabled = False
    settings.x402_payer_daily_spend_limit_usdc = 5_000_000

    with (
        patch("teardrop.mcp_gateway.get_settings", return_value=settings),
        patch("billing.get_tool_pricing_overrides", new=AsyncMock(return_value={})),
        patch("billing.get_current_pricing", new=AsyncMock(return_value=None)),
        patch("billing.resolve_tool_cost", new=AsyncMock(return_value=250)),
        patch("billing.reserve_payer_spend", new=AsyncMock(return_value=True)) as reserve_mock,
        patch("billing.verify_credit", new=AsyncMock()) as verify_mock,
    ):
        result = await gateway._billing_gate(request)

    assert result == (None, 250, "get_price", "req-1")
    assert isinstance(request.state.mcp_call_event_id, str)
    reserve_mock.assert_awaited_once_with("signed-payment", "0xabc", 250, 5_000_000)
    # x402 callers are not credit-verified.
    verify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_billing_gate_x402_skips_subscription_gate():
    """Marketplace subscription gate is a credit-rail concept; x402 must skip it."""
    gateway = MCPGatewayMiddleware(app=MagicMock())
    body = b'{"jsonrpc":"2.0","id":"req-2","method":"tools/call","params":{"name":"acme/tool"}}'
    request = _gate_request(body, is_x402=True, org_id=None)

    settings = MagicMock()
    settings.mcp_billing_enabled = True
    settings.marketplace_enabled = True
    settings.x402_payer_daily_spend_limit_usdc = 5_000_000

    with (
        patch("teardrop.mcp_gateway.get_settings", return_value=settings),
        patch("billing.get_tool_pricing_overrides", new=AsyncMock(return_value={})),
        patch("billing.get_current_pricing", new=AsyncMock(return_value=None)),
        patch("billing.resolve_tool_cost", new=AsyncMock(return_value=500)),
        patch("billing.reserve_payer_spend", new=AsyncMock(return_value=True)),
        patch("marketplace.check_org_subscription", new=AsyncMock()) as sub_mock,
    ):
        result = await gateway._billing_gate(request)

    assert result == (None, 500, "acme/tool", "req-2")
    sub_mock.assert_not_called()


@pytest.mark.asyncio
async def test_billing_gate_x402_rejects_payer_over_daily_cap():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    body = b'{"jsonrpc":"2.0","id":"req-cap","method":"tools/call","params":{"name":"get_price"}}'
    request = _gate_request(body, is_x402=True, org_id=None)
    settings = MagicMock(mcp_billing_enabled=True, marketplace_enabled=False)
    settings.x402_payer_daily_spend_limit_usdc = 5_000_000

    with (
        patch("teardrop.mcp_gateway.get_settings", return_value=settings),
        patch("billing.get_tool_pricing_overrides", new=AsyncMock(return_value={})),
        patch("billing.get_current_pricing", new=AsyncMock(return_value=None)),
        patch("billing.resolve_tool_cost", new=AsyncMock(return_value=500)),
        patch("billing.reserve_payer_spend", new=AsyncMock(return_value=False)),
        patch("billing.release_payment_nonce", new=AsyncMock()) as release_mock,
    ):
        result = await gateway._billing_gate(request)

    assert result.status_code == 429
    assert json.loads(result.body)["error"]["code"] == -32029
    assert getattr(request.state, "mcp_call_event_id", None) is None
    release_mock.assert_awaited_once_with("signed-payment")


@pytest.mark.asyncio
async def test_billing_gate_x402_rejects_missing_verified_payer():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    body = b'{"jsonrpc":"2.0","id":"req-payer","method":"tools/call","params":{"name":"get_price"}}'
    request = _gate_request(body, is_x402=True, org_id=None)
    request.state.x402_billing = BillingResult(payer="")
    settings = MagicMock(mcp_billing_enabled=True, marketplace_enabled=False)

    with (
        patch("teardrop.mcp_gateway.get_settings", return_value=settings),
        patch("billing.get_tool_pricing_overrides", new=AsyncMock(return_value={})),
        patch("billing.get_current_pricing", new=AsyncMock(return_value=None)),
        patch("billing.resolve_tool_cost", new=AsyncMock(return_value=500)),
        patch("billing.reserve_payer_spend", new=AsyncMock()) as reserve_mock,
        patch("billing.release_payment_nonce", new=AsyncMock()) as release_mock,
    ):
        result = await gateway._billing_gate(request)

    assert result.status_code == 402
    assert "payer identity" in json.loads(result.body)["error"]["message"].lower()
    reserve_mock.assert_not_awaited()
    release_mock.assert_awaited_once_with("signed-payment")


@pytest.mark.asyncio
async def test_billing_gate_credit_path_still_verifies():
    """Non-x402 org callers must still be credit-verified before execution."""
    gateway = MCPGatewayMiddleware(app=MagicMock())
    body = b'{"jsonrpc":"2.0","id":"req-3","method":"tools/call","params":{"name":"get_price"}}'
    request = _gate_request(body, is_x402=False, org_id="org-7")

    settings = MagicMock()
    settings.mcp_billing_enabled = True
    settings.marketplace_enabled = False

    with (
        patch("teardrop.mcp_gateway.get_settings", return_value=settings),
        patch("billing.get_tool_pricing_overrides", new=AsyncMock(return_value={})),
        patch("billing.get_current_pricing", new=AsyncMock(return_value=None)),
        patch("billing.resolve_tool_cost", new=AsyncMock(return_value=100)),
        patch("billing.verify_credit", new=AsyncMock(return_value=BillingResult(verified=True))) as verify_mock,
    ):
        result = await gateway._billing_gate(request)

    assert result == ("org-7", 100, "get_price", "req-3")
    assert isinstance(request.state.mcp_call_event_id, str)
    verify_mock.assert_awaited_once()


# ─── Settlement recovery: failed MCP settlements must be enqueued for retry ────


@pytest.mark.asyncio
async def test_settle_billing_credit_debit_fail_enqueues_recovery():
    """A failed credit debit (post-execution) must enqueue a recovery row."""
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock(spec=[])  # no x402_billing
    response = MagicMock()
    pending = ("org-1", 100, "test_tool", "req-1")

    with (
        patch("billing.debit_credit", new=AsyncMock(return_value=(False, 0))),
        patch("billing.settlement.enqueue_failed_settlement", new_callable=AsyncMock) as enqueue_mock,
        patch.object(gateway, "_record_mcp_outcome") as record_mock,
    ):
        result = await gateway._settle_billing(request, pending, response, execution_failed=False)

    enqueue_mock.assert_awaited_once()
    args = enqueue_mock.await_args.args
    # (usage_event_id, org_id, run_id, billing_method, amount_usdc)
    assert args[1] == "org-1"
    assert args[3] == "credit"
    assert args[4] == 100
    record_mock.assert_called_once_with(request, "org-1", "test_tool", 100, "credit", "failed")
    assert result is response


@pytest.mark.asyncio
async def test_settle_billing_x402_exception_enqueues_recovery():
    """An exception during x402 settlement must enqueue a recovery row."""
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock()
    request.state.x402_billing = MagicMock()
    request.state.x402_billing.payment_payload = "b64-payload"
    response = MagicMock()
    pending = ("org-1", 100, "acme/test_tool", "req-1")

    with (
        patch("billing.settle_payment", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch("billing.settlement.enqueue_failed_settlement", new_callable=AsyncMock) as enqueue_mock,
        patch.object(gateway, "_record_mcp_outcome") as record_mock,
    ):
        result = await gateway._settle_billing(request, pending, response, execution_failed=False)

    enqueue_mock.assert_awaited_once()
    args = enqueue_mock.await_args.args
    assert args[3] == "x402"
    assert args[4] == 100
    assert enqueue_mock.await_args.kwargs["payment_payload"] == "b64-payload"
    record_mock.assert_called_once_with(request, "org-1", "acme/test_tool", 100, "x402", "failed")
    assert result is response


@pytest.mark.asyncio
async def test_settle_billing_x402_rejected_enqueues_recovery():
    """A rejected x402 settlement must enqueue a recovery row."""
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock()
    request.state.x402_billing = MagicMock()
    request.state.x402_billing.payment_payload = None
    response = MagicMock()
    pending = ("org-1", 100, "acme/test_tool", "req-1")

    with (
        patch(
            "billing.settle_payment",
            new=AsyncMock(return_value=BillingResult(verified=True, settled=False, error="rejected")),
        ),
        patch("billing.settlement.enqueue_failed_settlement", new_callable=AsyncMock) as enqueue_mock,
        patch.object(gateway, "_record_mcp_outcome") as record_mock,
    ):
        result = await gateway._settle_billing(request, pending, response, execution_failed=False)

    enqueue_mock.assert_awaited_once()
    assert enqueue_mock.await_args.args[3] == "x402"
    record_mock.assert_called_once_with(request, "org-1", "acme/test_tool", 100, "x402", "failed")
    assert result is response


@pytest.mark.asyncio
async def test_settle_billing_credit_success_does_not_enqueue():
    """Regression: a successful credit debit must NOT enqueue recovery."""
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock(spec=[])
    response = MagicMock()
    pending = ("org-1", 100, "test_tool", "req-1")

    with (
        patch("billing.debit_credit", new=AsyncMock(return_value=(True, 100))),
        patch("billing.settlement.enqueue_failed_settlement", new_callable=AsyncMock) as enqueue_mock,
    ):
        result = await gateway._settle_billing(request, pending, response, execution_failed=False)

    enqueue_mock.assert_not_called()
    assert result is response
