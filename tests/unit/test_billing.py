"""Unit tests for billing.py — pure functions and mocked DB / x402 calls.

All external interactions (DB pool, x402 server, x402 parser) are mocked so
this suite runs without a live Postgres instance or a real blockchain node.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import billing as billing_module
from billing import (
    BillingResult,
    PricingRule,
    admin_topup_credit,
    atomic_usdc_to_price_str,
    calculate_run_cost_usdc,
    debit_credit,
    get_credit_balance,
    settle_payment,
    verify_credit,
    verify_payment,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_rule(**overrides) -> PricingRule:
    defaults = dict(
        id="rule-1",
        name="standard",
        run_price_usdc=10_000,
        tokens_in_cost_per_1k=0,
        tokens_out_cost_per_1k=0,
        tool_call_cost=0,
    )
    defaults.update(overrides)
    return PricingRule(**defaults)


# ─── atomic_usdc_to_price_str ─────────────────────────────────────────────────


class TestAtomicUsdcToPriceStr:
    def test_one_cent(self):
        assert atomic_usdc_to_price_str(10_000) == "$0.01"

    def test_one_dollar(self):
        assert atomic_usdc_to_price_str(1_000_000) == "$1.00"

    def test_fifty_cents(self):
        assert atomic_usdc_to_price_str(500_000) == "$0.50"

    def test_zero_keeps_two_decimal_places(self):
        result = atomic_usdc_to_price_str(0)
        assert result.startswith("$0.0")
        integer, frac = result.lstrip("$").split(".")
        assert len(frac) >= 2

    def test_ten_dollars(self):
        assert atomic_usdc_to_price_str(10_000_000) == "$10.00"

    def test_minimum_two_decimal_places(self):
        # 1 atomic unit = $0.000001 — must still have at least 2 decimal places
        result = atomic_usdc_to_price_str(1)
        integer, frac = result.lstrip("$").split(".")
        assert len(frac) >= 2

    def test_quarter_dollar(self):
        assert atomic_usdc_to_price_str(250_000) == "$0.25"


# ─── calculate_run_cost_usdc ──────────────────────────────────────────────────


class TestCalculateRunCostUsdc:
    async def test_flat_rate_when_no_per_unit_rates(self):
        rule = _make_rule(run_price_usdc=10_000)
        with patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)):
            cost = await calculate_run_cost_usdc(
                {"tokens_in": 5_000, "tokens_out": 2_000, "tool_calls": 3}
            )
        assert cost == 10_000

    async def test_usage_based_pricing_full_formula(self):
        rule = _make_rule(
            run_price_usdc=0,
            tokens_in_cost_per_1k=100,
            tokens_out_cost_per_1k=200,
            tool_call_cost=50,
        )
        with patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)):
            # (2000//1000)*100 + (1000//1000)*200 + 2*50 = 200+200+100 = 500
            cost = await calculate_run_cost_usdc(
                {"tokens_in": 2_000, "tokens_out": 1_000, "tool_calls": 2}
            )
        assert cost == 500

    async def test_floor_truncation_below_1k_tokens(self):
        """999 tokens = 0 completed 1k blocks — cost is 0 for that leg."""
        rule = _make_rule(run_price_usdc=0, tokens_in_cost_per_1k=100)
        with patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)):
            cost = await calculate_run_cost_usdc(
                {"tokens_in": 999, "tokens_out": 0, "tool_calls": 0}
            )
        assert cost == 0

    async def test_zero_usage_returns_flat_rate(self):
        rule = _make_rule(run_price_usdc=5_000)
        with patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)):
            cost = await calculate_run_cost_usdc(
                {"tokens_in": 0, "tokens_out": 0, "tool_calls": 0}
            )
        assert cost == 5_000

    async def test_no_pricing_rule_returns_zero(self):
        with patch("billing.get_live_pricing", new=AsyncMock(return_value=None)):
            cost = await calculate_run_cost_usdc(
                {"tokens_in": 1_000, "tokens_out": 500, "tool_calls": 1}
            )
        assert cost == 0

    async def test_tool_calls_only_with_per_unit_rate(self):
        rule = _make_rule(run_price_usdc=0, tool_call_cost=250)
        with patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)):
            cost = await calculate_run_cost_usdc(
                {"tokens_in": 0, "tokens_out": 0, "tool_calls": 4}
            )
        assert cost == 1_000

    async def test_missing_usage_keys_default_to_zero(self):
        """Empty dict should behave like zero-usage flat-rate run."""
        rule = _make_rule(run_price_usdc=5_000)
        with patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)):
            cost = await calculate_run_cost_usdc({})
        assert cost == 5_000

    async def test_usage_based_exactly_1k_tokens(self):
        rule = _make_rule(run_price_usdc=0, tokens_out_cost_per_1k=300)
        with patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)):
            cost = await calculate_run_cost_usdc(
                {"tokens_in": 0, "tokens_out": 1_000, "tool_calls": 0}
            )
        assert cost == 300


# ─── verify_credit ────────────────────────────────────────────────────────────


class TestVerifyCredit:
    async def test_sufficient_balance_returns_verified(self):
        with patch("billing.get_credit_balance", new=AsyncMock(return_value=50_000)):
            result = await verify_credit("org-1", 10_000)
        assert result.verified is True
        assert result.billing_method == "credit"
        assert result.error == ""

    async def test_insufficient_balance_returns_not_verified(self):
        with patch("billing.get_credit_balance", new=AsyncMock(return_value=5_000)):
            result = await verify_credit("org-1", 10_000)
        assert result.verified is False
        assert "Insufficient credit" in result.error
        assert "5000" in result.error

    async def test_exact_balance_passes(self):
        with patch("billing.get_credit_balance", new=AsyncMock(return_value=10_000)):
            result = await verify_credit("org-1", 10_000)
        assert result.verified is True

    async def test_zero_min_balance_always_passes(self):
        with patch("billing.get_credit_balance", new=AsyncMock(return_value=0)):
            result = await verify_credit("org-1", 0)
        assert result.verified is True

    async def test_error_message_includes_required_amount(self):
        with patch("billing.get_credit_balance", new=AsyncMock(return_value=0)):
            result = await verify_credit("org-1", 25_000)
        assert "25000" in result.error


# ─── get_credit_balance ───────────────────────────────────────────────────────


class TestGetCreditBalance:
    async def test_returns_balance_from_row(self):
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value={"balance_usdc": 42_000})
        with patch.object(billing_module, "_pool", mock_pool):
            balance = await get_credit_balance("org-1")
        assert balance == 42_000

    async def test_returns_zero_when_no_row(self):
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(billing_module, "_pool", mock_pool):
            balance = await get_credit_balance("org-1")
        assert balance == 0


# ─── verify_payment ───────────────────────────────────────────────────────────
#
# verify_payment does lazy `from x402 import parse_payment_payload` inside the
# function body.  We inject a fake x402 module via sys.modules so the import
# resolves without the real library being installed.


def _payment_fixture(mock_parse_return=None, mock_parse_side_effect=None):
    """Return (mock_server, mock_req, mock_x402) ready for patching."""
    mock_x402 = MagicMock()
    if mock_parse_side_effect is not None:
        mock_x402.parse_payment_payload.side_effect = mock_parse_side_effect
    else:
        mock_x402.parse_payment_payload.return_value = (
            mock_parse_return or MagicMock()
        )
    mock_server = MagicMock()
    mock_req = MagicMock()
    return mock_server, mock_req, mock_x402


class TestVerifyPayment:
    async def test_malformed_header_returns_error(self):
        """Exception from parse_payment_payload returns BillingResult with error."""
        mock_server, mock_req, mock_x402 = _payment_fixture(
            mock_parse_side_effect=Exception("bad parse")
        )
        with (
            patch.object(billing_module, "_server", mock_server),
            patch.object(billing_module, "_requirements_cache", [mock_req]),
            patch("billing._rebuild_requirements_if_stale", new=AsyncMock()),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_payment(
                base64.b64encode(b"bad data").decode()
            )
        assert result.verified is False
        assert "Malformed payment header" in result.error

    async def test_empty_requirements_returns_error(self):
        """Empty requirements cache should not call server.verify_payment."""
        mock_server = MagicMock()
        mock_x402 = MagicMock()
        with (
            patch.object(billing_module, "_server", mock_server),
            patch.object(billing_module, "_requirements_cache", []),
            patch("billing._rebuild_requirements_if_stale", new=AsyncMock()),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_payment(base64.b64encode(b"anything").decode())
        assert result.verified is False
        assert "No payment requirements" in result.error
        mock_server.verify_payment.assert_not_called()

    async def test_invalid_signature_surfaces_reason(self):
        """Server returning is_valid=False includes the reason in the error."""
        mock_verify = MagicMock(
            is_valid=False, invalid_reason="wrong amount", payer="0xabc", invalid_message=None
        )
        mock_server, mock_req, mock_x402 = _payment_fixture()
        mock_server.verify_payment = AsyncMock(return_value=mock_verify)
        with (
            patch.object(billing_module, "_server", mock_server),
            patch.object(billing_module, "_requirements_cache", [mock_req]),
            patch("billing._rebuild_requirements_if_stale", new=AsyncMock()),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_payment(base64.b64encode(b"valid").decode())
        assert result.verified is False
        assert "wrong amount" in result.error

    async def test_valid_payment_returns_verified(self):
        """Server returning is_valid=True sets verified=True and stores payload."""
        mock_payload = MagicMock()
        mock_verify = MagicMock(is_valid=True)
        mock_server, mock_req, mock_x402 = _payment_fixture(
            mock_parse_return=mock_payload
        )
        mock_server.verify_payment = AsyncMock(return_value=mock_verify)
        with (
            patch.object(billing_module, "_server", mock_server),
            patch.object(billing_module, "_requirements_cache", [mock_req]),
            patch("billing._rebuild_requirements_if_stale", new=AsyncMock()),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_payment(base64.b64encode(b"valid").decode())
        assert result.verified is True
        assert result.payment_payload is mock_payload
        assert result.payment_requirements is mock_req

    async def test_server_exception_returns_error(self):
        """Exception from server.verify_payment is caught and returns error."""
        mock_server, mock_req, mock_x402 = _payment_fixture()
        mock_server.verify_payment = AsyncMock(
            side_effect=Exception("connection refused")
        )
        with (
            patch.object(billing_module, "_server", mock_server),
            patch.object(billing_module, "_requirements_cache", [mock_req]),
            patch("billing._rebuild_requirements_if_stale", new=AsyncMock()),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_payment(base64.b64encode(b"valid").decode())
        assert result.verified is False
        assert "connection refused" in result.error


# ─── settle_payment ───────────────────────────────────────────────────────────


class TestSettlePayment:
    async def test_settle_unverified_returns_error(self):
        result = await settle_payment(BillingResult(verified=False))
        assert result.verified is False
        assert "Cannot settle unverified" in result.error

    async def test_settlement_success_sets_tx_hash(self):
        mock_settle = MagicMock(success=True, tx_hash="0xdeadbeef", transaction_hash=None)
        mock_server = MagicMock()
        mock_server.settle_payment = AsyncMock(return_value=mock_settle)
        verified = BillingResult(
            verified=True,
            payment_payload=MagicMock(),
            payment_requirements=MagicMock(amount="10000"),
        )
        with patch.object(billing_module, "_server", mock_server):
            result = await settle_payment(verified)
        assert result.settled is True
        assert result.tx_hash == "0xdeadbeef"

    async def test_settlement_rejected_by_facilitator(self):
        mock_settle = MagicMock(success=False)
        mock_server = MagicMock()
        mock_server.settle_payment = AsyncMock(return_value=mock_settle)
        verified = BillingResult(
            verified=True,
            payment_payload=MagicMock(),
            payment_requirements=MagicMock(amount="10000"),
        )
        with patch.object(billing_module, "_server", mock_server):
            result = await settle_payment(verified)
        assert result.settled is False
        assert "Settlement rejected" in result.error

    async def test_settle_exception_returns_error(self):
        mock_server = MagicMock()
        mock_server.settle_payment = AsyncMock(
            side_effect=Exception("network error")
        )
        verified = BillingResult(
            verified=True,
            payment_payload=MagicMock(),
            payment_requirements=MagicMock(amount="10000"),
        )
        with patch.object(billing_module, "_server", mock_server):
            result = await settle_payment(verified)
        assert result.settled is False
        assert "network error" in result.error


# ─── DB-layer functions (pool mocked) ────────────────────────────────────────


def _pool_mock():
    """Return a MagicMock that can be wired as billing._pool."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.execute = AsyncMock()
    conn = MagicMock()
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_async_ctx(conn))
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    pool._conn = conn  # for assertion access
    return pool


class _async_ctx:
    """Minimal async context manager that returns *value* on __aenter__."""
    def __init__(self, value):
        self._value = value
    async def __aenter__(self):
        return self._value
    async def __aexit__(self, *args):
        pass


@pytest.mark.anyio
class TestGetCurrentPricing:
    async def test_returns_pricing_rule_when_row_exists(self):
        from billing import get_current_pricing, PricingRule
        from datetime import datetime, timezone

        pool = _pool_mock()
        pool.fetchrow = AsyncMock(return_value={
            "id": "p1", "name": "Standard", "run_price_usdc": 10_000,
            "tokens_in_cost_per_1k": 0, "tokens_out_cost_per_1k": 0,
            "tool_call_cost": 0,
            "effective_from": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
        })
        with patch.object(billing_module, "_pool", pool):
            rule = await get_current_pricing()
        assert isinstance(rule, PricingRule)
        assert rule.run_price_usdc == 10_000

    async def test_returns_none_when_no_row(self):
        from billing import get_current_pricing

        pool = _pool_mock()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(billing_module, "_pool", pool):
            rule = await get_current_pricing()
        assert rule is None


@pytest.mark.anyio
class TestGetBillingHistory:
    async def test_returns_empty_list_when_no_rows(self):
        from billing import get_billing_history

        pool = _pool_mock()
        pool.fetch = AsyncMock(return_value=[])
        with patch.object(billing_module, "_pool", pool):
            history = await get_billing_history("user-1", limit=10)
        assert history == []

    async def test_cursor_pagination_path(self):
        from billing import get_billing_history
        from datetime import datetime, timezone

        pool = _pool_mock()
        pool.fetch = AsyncMock(return_value=[])
        cursor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(billing_module, "_pool", pool):
            await get_billing_history("user-1", limit=5, cursor=cursor)
        # cursor-based query passes cursor as third param
        call_args = pool.fetch.call_args.args
        assert cursor in call_args


@pytest.mark.anyio
class TestGetInvoices:
    async def test_no_cursor_path(self):
        from billing import get_invoices

        pool = _pool_mock()
        pool.fetch = AsyncMock(return_value=[])
        with patch.object(billing_module, "_pool", pool):
            result = await get_invoices("user-1")
        assert result == []

    async def test_cursor_pagination_path(self):
        from billing import get_invoices
        from datetime import datetime, timezone

        pool = _pool_mock()
        pool.fetch = AsyncMock(return_value=[])
        cursor = datetime(2026, 3, 1, tzinfo=timezone.utc)
        with patch.object(billing_module, "_pool", pool):
            await get_invoices("user-1", cursor=cursor)
        call_args = pool.fetch.call_args.args
        assert cursor in call_args


@pytest.mark.anyio
class TestGetInvoiceByRun:
    async def test_returns_dict_when_found(self):
        from billing import get_invoice_by_run
        from datetime import datetime, timezone

        row = {
            "id": "e1", "run_id": "r1", "thread_id": "t1",
            "tokens_in": 100, "tokens_out": 50, "tool_calls": 1,
            "tool_names": ["get_datetime"], "duration_ms": 200,
            "cost_usdc": 10_000, "settlement_tx": "0xfoo",
            "settlement_status": "settled",
            "created_at": datetime.now(timezone.utc),
        }
        pool = _pool_mock()
        pool.fetchrow = AsyncMock(return_value=row)
        with patch.object(billing_module, "_pool", pool):
            result = await get_invoice_by_run("r1", "user-1")
        assert result["run_id"] == "r1"

    async def test_returns_none_when_not_found(self):
        from billing import get_invoice_by_run

        pool = _pool_mock()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(billing_module, "_pool", pool):
            result = await get_invoice_by_run("missing-run", "user-1")
        assert result is None


@pytest.mark.anyio
class TestAdminTopupCredit:
    async def test_returns_new_balance(self):
        from billing import admin_topup_credit

        pool = _pool_mock()
        pool.fetchrow = AsyncMock(return_value={"balance_usdc": 50_000})
        with patch.object(billing_module, "_pool", pool):
            balance = await admin_topup_credit("org-1", 50_000)
        assert balance == 50_000


@pytest.mark.anyio
class TestDebitCreditMock:
    async def test_debit_returns_false_when_no_row(self):
        from billing import debit_credit

        pool = _pool_mock()
        pool._conn.fetchrow = AsyncMock(return_value=None)
        with patch.object(billing_module, "_pool", pool):
            result = await debit_credit("org-missing", 5_000)
        assert result is False

    async def test_debit_returns_true_on_success(self):
        from billing import debit_credit

        pool = _pool_mock()
        pool._conn.fetchrow = AsyncMock(return_value={"balance_usdc": 20_000})
        pool._conn.execute = AsyncMock()
        with patch.object(billing_module, "_pool", pool):
            result = await debit_credit("org-1", 5_000)
        assert result is True

    async def test_debit_returns_false_on_db_exception(self):
        from billing import debit_credit

        pool = MagicMock()
        pool.acquire = MagicMock(side_effect=Exception("DB connection lost"))
        with patch.object(billing_module, "_pool", pool):
            result = await debit_credit("org-1", 5_000)
        assert result is False


@pytest.mark.anyio
class TestRecordSettlement:
    async def test_calls_execute_with_correct_args(self):
        from billing import record_settlement

        pool = _pool_mock()
        pool.execute = AsyncMock()
        with patch.object(billing_module, "_pool", pool):
            await record_settlement("evt-1", 10_000, "0xhash", "settled")
        pool.execute.assert_called_once()
        call_args = pool.execute.call_args.args
        assert "evt-1" in call_args
        assert 10_000 in call_args
        assert "0xhash" in call_args

    async def test_exception_is_swallowed(self):
        from billing import record_settlement

        pool = _pool_mock()
        pool.execute = AsyncMock(side_effect=Exception("DB down"))
        with patch.object(billing_module, "_pool", pool):
            # Should not raise
            await record_settlement("evt-1", 0, "0x", "failed")
