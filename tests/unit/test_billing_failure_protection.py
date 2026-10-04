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
from teardrop.mcp_gateway import (
    MCPGatewayMiddleware,
    _anonymous_failure_budget,
    _payer_failure_budget,
    _record_unbilled_failure,
)


@pytest.mark.asyncio
async def test_record_mcp_outcome_schedules_sanitized_x402_event():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state.mcp_call_event_id = "server-call-1"
    request.state.x402_billing = BillingResult(payer="0xabc", payment_payload="secret-payload")

    with patch("teardrop.usage.record_mcp_call_event", new_callable=AsyncMock) as record_mock:
        await gateway._record_mcp_outcome(request, None, "platform/get_price", 200, "x402", "settled", "0xtx")
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


def _assert_withheld(result, req_id: str) -> None:
    body = json.loads(result.body)
    assert result.status_code == 200
    assert body["id"] == req_id
    assert body["result"]["isError"] is True
    assert "withheld" in body["result"]["structuredContent"]["error"]


@pytest.fixture
def _stub_402_body():
    with patch("billing.build_402_response_body", side_effect=lambda **kw: {"x402Version": 2, "accepts": [], **kw}):
        yield


@pytest.mark.asyncio
async def test_settle_billing_x402_rejected_skips_earnings(_stub_402_body):
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock()
    request.state.x402_billing = MagicMock()
    request.state.mcp_x402_challenge = (True, {})
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
    _assert_withheld(result, "req-1")


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
async def test_response_indicates_failure_detects_jsonrpc_error():
    """JSON-RPC error envelopes (HTTP 200 in json_response mode) skip billing."""
    response = MagicMock()

    async def _iter():
        yield b'{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"Invalid params"}}'

    response.body_iterator = _iter()
    assert await MCPGatewayMiddleware._response_indicates_failure(response) is True


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
async def test_x402_auth_rejects_community_tool_before_payment():
    """Community tools are credit-only: no x402 challenge, verify, or nonce claim."""
    gateway = MCPGatewayMiddleware(app=MagicMock())
    body = b'{"jsonrpc":"2.0","id":"req-2","method":"tools/call","params":{"name":"acme/tool"}}'
    request = _gate_request(body, is_x402=False, org_id=None)

    with patch("billing.verify_payment", new=AsyncMock()) as verify_mock:
        result = await gateway._handle_x402_auth(request)

    assert result.status_code == 401
    assert json.loads(result.body)["error"]["code"] == -32001
    assert "Bearer" in result.headers["WWW-Authenticate"]
    verify_mock.assert_not_called()


async def _run_credit_gate(name: str, *, row, meta=None, cost=500, org_id="org-1"):
    gateway = MCPGatewayMiddleware(app=MagicMock())
    params = {"name": name, **({"_meta": meta} if meta is not None else {})}
    body = json.dumps({"jsonrpc": "2.0", "id": "req-3", "method": "tools/call", "params": params}).encode()
    request = _gate_request(body, is_x402=False, org_id=org_id)
    settings = MagicMock(mcp_billing_enabled=True, onboarding_credit_enabled=False, marketplace_enabled=True)
    verify = AsyncMock(return_value=BillingResult(verified=True))
    with (
        patch("teardrop.mcp_gateway.get_settings", return_value=settings),
        patch("billing.get_tool_pricing_overrides", new=AsyncMock(return_value={})),
        patch("billing.get_current_pricing", new=AsyncMock(return_value=None)),
        patch("billing.resolve_tool_cost", new=AsyncMock(return_value=cost)),
        patch("marketplace.get_marketplace_tool_by_name", new=AsyncMock(return_value=row)),
        patch("billing.verify_credit", new=verify),
    ):
        result = await gateway._billing_gate(request)
    return result, verify


@pytest.mark.asyncio
async def test_billing_gate_credit_community_tool_verifies_credit():
    result, verify = await _run_credit_gate("acme/tool", row={"org_id": "author-org"})

    assert result == ("org-1", 500, "acme/tool", "req-3")
    verify.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "status", "code"),
    [(None, 404, -32601), ({"org_id": "org-1"}, 403, -32005)],
)
async def test_billing_gate_credit_rejects_unknown_or_self_owned_community_tool(row, status, code):
    result, verify = await _run_credit_gate("acme/tool", row=row)

    assert result.status_code == status
    assert json.loads(result.body)["error"]["code"] == code
    verify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cap", "status", "code"),
    [(499, 402, -32004), (-1, 400, -32602), ("500", 400, -32602), (True, 400, -32602), (1.5, 400, -32602)],
)
async def test_billing_gate_credit_enforces_max_cost_before_verify(cap, status, code):
    result, verify = await _run_credit_gate("get_price", row=None, meta={"teardrop/max_cost_usdc": cap})

    assert result.status_code == status
    assert json.loads(result.body)["error"]["code"] == code
    verify.assert_not_called()


@pytest.mark.asyncio
async def test_billing_gate_credit_max_cost_equal_to_price_passes():
    result, verify = await _run_credit_gate("get_price", row=None, meta={"teardrop/max_cost_usdc": 500})

    assert result == ("org-1", 500, "get_price", "req-3")
    verify.assert_awaited_once()


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


# ─── MPP is prepaid: the billing gate must never refuse it after payment ─────
# The payer's transfer is final before execution. x402's payer cap and failure
# budget bound unsettled exposure; applying them here would keep funds without
# service, and releasing the mpp:<tx> claim would let the transfer replay.

_MPP_PAYER = "0x" + "42" * 20
_MPP_GATE_BODY = b'{"jsonrpc":"2.0","id":"req-mpp","method":"tools/call","params":{"name":"get_price"}}'


def _mpp_gate_request():
    request = MagicMock()
    request.method = "POST"
    request.headers = {}
    request.body = AsyncMock(return_value=_MPP_GATE_BODY)
    request.state = SimpleNamespace(
        mcp_org_id=None,
        x402_billing=BillingResult(
            verified=True, settled=True, billing_method="mpp", payer=_MPP_PAYER, amount_usdc=500, tx_hash="0x" + "11" * 32
        ),
    )
    return request


@pytest.mark.asyncio
async def test_billing_gate_mpp_skips_post_payment_refusals():
    from teardrop.rate_limit import record_auth_failure

    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = _mpp_gate_request()
    settings = MagicMock(mcp_billing_enabled=True, marketplace_enabled=False)
    settings.x402_payer_daily_spend_limit_usdc = 5_000_000

    with (
        patch("teardrop.mcp_gateway.get_settings", return_value=settings),
        patch("billing.get_tool_pricing_overrides", new=AsyncMock(return_value={})),
        patch("billing.get_current_pricing", new=AsyncMock(return_value=None)),
        patch("billing.resolve_tool_cost", new=AsyncMock(return_value=500)),
        patch("billing.reserve_payer_spend", new=AsyncMock(return_value=False)) as reserve_mock,
        patch("billing.release_payment_nonce", new=AsyncMock()) as release_mock,
    ):
        for _ in range(3):
            await record_auth_failure(f"mcpfail:payer:{_MPP_PAYER}", 600)
        result = await gateway._billing_gate(request)

    assert result == (None, 500, "get_price", "req-mpp")
    reserve_mock.assert_not_awaited()
    release_mock.assert_not_awaited()


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
async def test_mcp_credit_recovery_persists_charge_before_enqueue():
    from billing.charges import charge_id_for

    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = SimpleNamespace(mcp_call_event_id="call-1", mcp_principal_id="user-1")
    order: list[str] = []

    async def _charge(**kwargs):
        order.append("charge")
        return charge_id_for(kwargs["source"], kwargs["invocation_id"])

    async def _enqueue(*args, **kwargs):
        order.append("enqueue")

    with (
        patch("billing.debit_credit", new=AsyncMock(return_value=(False, 0))),
        patch("teardrop.usage.record_mcp_call_event", new_callable=AsyncMock),
        patch("billing.charges.record_charge", new=AsyncMock(side_effect=_charge)) as charge_mock,
        patch("billing.settlement.enqueue_failed_settlement", new=AsyncMock(side_effect=_enqueue)) as enqueue_mock,
    ):
        await gateway._settle_billing(request, ("org-1", 100, "acme/tool", "req-1"), MagicMock())

    assert order == ["charge", "enqueue"]
    charge_mock.assert_awaited_once()
    assert charge_mock.await_args.kwargs["status"] == "failed"
    args = enqueue_mock.await_args.args
    assert args[0] == args[2] == "call-1"
    assert enqueue_mock.await_args.kwargs["charge_id"] == charge_id_for("mcp", "call-1")


@pytest.mark.asyncio
async def test_record_mcp_outcome_dual_writes_charge_with_call_event_id():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state.mcp_call_event_id = "server-call-1"
    request.state.mcp_principal_id = "user-1"
    request.state.x402_billing = None

    with (
        patch("teardrop.usage.record_mcp_call_event", new_callable=AsyncMock),
        patch("billing.charges.record_charge", new_callable=AsyncMock) as charge_mock,
    ):
        await gateway._record_mcp_outcome(request, "org-1", "acme/tool", 100, "credit", "settled")

    kwargs = charge_mock.await_args.kwargs
    assert (kwargs["source"], kwargs["invocation_id"]) == ("mcp", "server-call-1")
    assert (kwargs["status"], kwargs["settled_amount_usdc"], kwargs["principal_id"]) == ("settled", 100, "user-1")


@pytest.mark.asyncio
async def test_record_mcp_outcome_mpp_passes_payer_and_tx():
    """F3: mpp outcomes must carry the verified payer address + tx to consumers,
    exactly like x402 — not the x402-only payer mapping that dropped both."""
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state.mcp_call_event_id = "server-call-1"
    request.state.x402_billing = BillingResult(
        verified=True,
        settled=True,
        billing_method="mpp",
        payer="did:pkh:eip155:8453:0x" + "42" * 20,
        tx_hash="0x" + "11" * 32,
    )
    tx = "0x" + "11" * 32

    with (
        patch("teardrop.usage.record_mcp_call_event", new_callable=AsyncMock) as record_mock,
        patch("billing.charges.record_charge", new_callable=AsyncMock, return_value="charge-1") as charge_mock,
    ):
        await gateway._record_mcp_outcome(request, None, "calculate", 10_000, "mpp", "settled", tx)
        await asyncio.sleep(0)

    record_mock.assert_awaited_once_with(
        "server-call-1",
        "",
        "did:pkh:eip155:8453:0x" + "42" * 20,
        "calculate",
        "mpp",
        10_000,
        "settled",
        tx,
    )
    kwargs = charge_mock.await_args.kwargs
    assert kwargs["payer_address"] == "did:pkh:eip155:8453:0x" + "42" * 20
    assert kwargs["billing_method"] == "mpp"
    assert kwargs["settlement_tx"] == tx


@pytest.mark.asyncio
async def test_settle_billing_x402_exception_withholds_result_without_recovery(_stub_402_body):
    """A withheld result must never be retried into a charge."""
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock()
    request.state.x402_billing = MagicMock()
    request.state.x402_billing.payment_payload = "b64-payload"
    request.state.mcp_x402_challenge = (True, {})
    response = MagicMock()
    pending = ("org-1", 100, "acme/test_tool", "req-1")

    with (
        patch("billing.settle_payment", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch("billing.settlement.enqueue_failed_settlement", new_callable=AsyncMock) as enqueue_mock,
        patch.object(gateway, "_record_mcp_outcome") as record_mock,
    ):
        result = await gateway._settle_billing(request, pending, response, execution_failed=False)

    enqueue_mock.assert_not_called()
    record_mock.assert_called_once_with(request, "org-1", "acme/test_tool", 100, "x402", "failed")
    _assert_withheld(result, "req-1")


@pytest.mark.asyncio
async def test_settle_billing_x402_rejected_withholds_result_without_recovery(_stub_402_body):
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock()
    request.state.x402_billing = MagicMock()
    request.state.x402_billing.payment_payload = None
    request.state.mcp_x402_challenge = (True, {})
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

    enqueue_mock.assert_not_called()
    record_mock.assert_called_once_with(request, "org-1", "acme/test_tool", 100, "x402", "failed")
    _assert_withheld(result, "req-1")


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


# ─── Unbilled-failure budget (build order §3.1/§5.2) ─────────────────────────


@pytest.fixture(autouse=True)
def _clear_failure_budget():
    from teardrop import rate_limit

    rate_limit._auth_fail_counters.clear()
    yield
    rate_limit._auth_fail_counters.clear()


@pytest.mark.asyncio
async def test_anonymous_failure_budget_clear_below_limit():
    request = MagicMock()
    with patch("teardrop.mcp_gateway.client_ip_from_request", return_value="203.0.113.7"):
        assert await _anonymous_failure_budget(request, 1) is None


@pytest.mark.asyncio
async def test_anonymous_failure_budget_locks_after_three_failures():
    request = MagicMock()
    request.state = MagicMock(spec=[])
    with patch("teardrop.mcp_gateway.client_ip_from_request", return_value="203.0.113.7"):
        for _ in range(3):
            await _record_unbilled_failure(request)
        limited = await _anonymous_failure_budget(request, 1)

    assert limited is not None
    assert limited.status_code == 429
    assert limited.headers["X-RateLimit-Scope"] == "failure-budget"
    assert json.loads(limited.body)["error"]["code"] == -32029


@pytest.mark.asyncio
async def test_payer_failure_budget_locks_independent_of_ip():
    request = MagicMock()
    request.state.x402_billing = SimpleNamespace(payer="0xDEAD")
    ips = iter(["1.1.1.1", "2.2.2.2", "3.3.3.3"])
    with patch("teardrop.mcp_gateway.client_ip_from_request", side_effect=lambda *a, **k: next(ips)):
        for _ in range(3):
            await _record_unbilled_failure(request)
    with patch("teardrop.mcp_gateway.client_ip_from_request", return_value="4.4.4.4"):
        assert await _anonymous_failure_budget(request, 1) is None

    limited = await _payer_failure_budget("0xdead", 1)
    assert limited is not None
    assert limited.status_code == 429
    assert limited.headers["X-RateLimit-Scope"] == "x402-payer"


@pytest.mark.asyncio
async def test_record_unbilled_failure_never_raises():
    request = MagicMock()
    with patch("teardrop.mcp_gateway.client_ip_from_request", side_effect=RuntimeError("boom")):
        await _record_unbilled_failure(request)  # must not raise


@pytest.mark.asyncio
async def test_settle_billing_failed_execution_records_failure():
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state = MagicMock(spec=[])
    response = MagicMock()
    pending = ("org-1", 100, "test_tool", "req-1")

    with patch("teardrop.mcp_gateway._record_unbilled_failure", new_callable=AsyncMock) as count_mock:
        result = await gateway._settle_billing(request, pending, response, execution_failed=True)

    count_mock.assert_awaited_once_with(request)
    assert result is response


@pytest.mark.asyncio
async def test_settle_billing_x402_rejected_records_failure(_stub_402_body):
    """Unfunded wallet: verify ok, settle fails, tool ran unbilled → budget counts it."""
    gateway = MCPGatewayMiddleware(app=MagicMock())
    request = MagicMock()
    request.state.x402_billing = MagicMock()
    request.state.x402_billing.payment_payload = None
    request.state.mcp_x402_challenge = (True, {})
    response = MagicMock()
    pending = ("org-1", 100, "acme/test_tool", "req-1")

    with (
        patch(
            "billing.settle_payment",
            new=AsyncMock(return_value=BillingResult(verified=True, settled=False, error="rejected")),
        ),
        patch("billing.settlement.enqueue_failed_settlement", new_callable=AsyncMock),
        patch.object(gateway, "_record_mcp_outcome"),
        patch("teardrop.mcp_gateway._record_unbilled_failure", new_callable=AsyncMock) as count_mock,
    ):
        result = await gateway._settle_billing(request, pending, response, execution_failed=False)

    count_mock.assert_awaited_once_with(request)
    _assert_withheld(result, "req-1")
