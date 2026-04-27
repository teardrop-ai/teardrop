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
    build_usdc_topup_requirements,
    calculate_run_cost_usdc,
    credit_usdc_topup,
    debit_credit,
    get_credit_balance,
    settle_payment,
    verify_and_settle_usdc_topup,
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
            cost = await calculate_run_cost_usdc({"tokens_in": 5_000, "tokens_out": 2_000, "tool_calls": 3})
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
            cost = await calculate_run_cost_usdc({"tokens_in": 2_000, "tokens_out": 1_000, "tool_calls": 2})
        assert cost == 500

    async def test_floor_truncation_below_1k_tokens(self):
        """999 tokens = 0 completed 1k blocks — cost is 0 for that leg."""
        rule = _make_rule(run_price_usdc=0, tokens_in_cost_per_1k=100)
        with patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)):
            cost = await calculate_run_cost_usdc({"tokens_in": 999, "tokens_out": 0, "tool_calls": 0})
        assert cost == 0

    async def test_zero_usage_returns_flat_rate(self):
        rule = _make_rule(run_price_usdc=5_000)
        with patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)):
            cost = await calculate_run_cost_usdc({"tokens_in": 0, "tokens_out": 0, "tool_calls": 0})
        assert cost == 5_000

    async def test_no_pricing_rule_returns_zero(self):
        with patch("billing.get_live_pricing", new=AsyncMock(return_value=None)):
            cost = await calculate_run_cost_usdc({"tokens_in": 1_000, "tokens_out": 500, "tool_calls": 1})
        assert cost == 0

    async def test_tool_calls_only_with_per_unit_rate(self):
        rule = _make_rule(run_price_usdc=0, tool_call_cost=250)
        with patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)):
            cost = await calculate_run_cost_usdc({"tokens_in": 0, "tokens_out": 0, "tool_calls": 4})
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
            cost = await calculate_run_cost_usdc({"tokens_in": 0, "tokens_out": 1_000, "tool_calls": 0})
        assert cost == 300


# ─── verify_credit ────────────────────────────────────────────────────────────


def _mock_pool_for_verify(balance=50_000, spending_limit=0, is_paused=False, daily_spend=0):
    """Create a mock pool that returns the given credit row and daily spend."""
    credit_row = {"balance_usdc": balance, "spending_limit_usdc": spending_limit, "is_paused": is_paused}
    daily_row = {"daily_spend": daily_spend}
    mock_pool = MagicMock()
    mock_pool.fetchrow = AsyncMock(side_effect=[credit_row, daily_row])
    return mock_pool


class TestVerifyCredit:
    async def test_sufficient_balance_returns_verified(self):
        mock_pool = _mock_pool_for_verify(balance=50_000)
        with patch.object(billing_module, "_pool", mock_pool):
            result = await verify_credit("org-1", 10_000)
        assert result.verified is True
        assert result.billing_method == "credit"
        assert result.error == ""

    async def test_insufficient_balance_returns_not_verified(self):
        mock_pool = _mock_pool_for_verify(balance=5_000)
        with patch.object(billing_module, "_pool", mock_pool):
            result = await verify_credit("org-1", 10_000)
        assert result.verified is False
        assert "Insufficient credit" in result.error
        assert "5000" in result.error

    async def test_exact_balance_passes(self):
        mock_pool = _mock_pool_for_verify(balance=10_000)
        with patch.object(billing_module, "_pool", mock_pool):
            result = await verify_credit("org-1", 10_000)
        assert result.verified is True

    async def test_zero_min_balance_always_passes(self):
        mock_pool = _mock_pool_for_verify(balance=0)
        with patch.object(billing_module, "_pool", mock_pool):
            result = await verify_credit("org-1", 0)
        assert result.verified is True

    async def test_error_message_includes_required_amount(self):
        mock_pool = _mock_pool_for_verify(balance=0)
        with patch.object(billing_module, "_pool", mock_pool):
            result = await verify_credit("org-1", 25_000)
        assert "25000" in result.error

    async def test_paused_org_returns_error(self):
        mock_pool = _mock_pool_for_verify(balance=50_000, is_paused=True)
        with patch.object(billing_module, "_pool", mock_pool):
            result = await verify_credit("org-1", 10_000)
        assert result.verified is False
        assert "paused" in result.error.lower()

    async def test_daily_spending_limit_exceeded(self):
        mock_pool = _mock_pool_for_verify(
            balance=100_000,
            spending_limit=50_000,
            daily_spend=45_000,
        )
        with patch.object(billing_module, "_pool", mock_pool):
            result = await verify_credit("org-1", 10_000)
        assert result.verified is False
        assert "spending limit" in result.error.lower()

    async def test_daily_spending_limit_within_budget(self):
        mock_pool = _mock_pool_for_verify(
            balance=100_000,
            spending_limit=50_000,
            daily_spend=30_000,
        )
        with patch.object(billing_module, "_pool", mock_pool):
            result = await verify_credit("org-1", 10_000)
        assert result.verified is True


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
        mock_x402.parse_payment_payload.return_value = mock_parse_return or MagicMock()
    mock_server = MagicMock()
    mock_req = MagicMock()
    mock_req.scheme = "exact"
    return mock_server, mock_req, mock_x402


class TestVerifyPayment:
    async def test_malformed_header_returns_error(self):
        """Exception from parse_payment_payload returns BillingResult with error."""
        mock_server, mock_req, mock_x402 = _payment_fixture(mock_parse_side_effect=Exception("bad parse"))
        with (
            patch.object(billing_module, "_server", mock_server),
            patch.object(billing_module, "_requirements_cache", [mock_req]),
            patch("billing._rebuild_requirements_if_stale", new=AsyncMock()),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_payment(base64.b64encode(b"bad data").decode())
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
        mock_verify = MagicMock(is_valid=False, invalid_reason="wrong amount", payer="0xabc", invalid_message=None)
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
        mock_server, mock_req, mock_x402 = _payment_fixture(mock_parse_return=mock_payload)
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
        mock_server.verify_payment = AsyncMock(side_effect=Exception("connection refused"))
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
        mock_server.settle_payment = AsyncMock(side_effect=Exception("network error"))
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
        from datetime import datetime, timezone

        from billing import PricingRule, get_current_pricing

        pool = _pool_mock()
        pool.fetchrow = AsyncMock(
            return_value={
                "id": "p1",
                "name": "Standard",
                "run_price_usdc": 10_000,
                "tokens_in_cost_per_1k": 0,
                "tokens_out_cost_per_1k": 0,
                "tool_call_cost": 0,
                "effective_from": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc),
            }
        )
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
        from datetime import datetime, timezone

        from billing import get_billing_history

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
        from datetime import datetime, timezone

        from billing import get_invoices

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
        from datetime import datetime, timezone

        from billing import get_invoice_by_run

        row = {
            "id": "e1",
            "run_id": "r1",
            "thread_id": "t1",
            "tokens_in": 100,
            "tokens_out": 50,
            "tool_calls": 1,
            "tool_names": ["get_datetime"],
            "duration_ms": 200,
            "cost_usdc": 10_000,
            "settlement_tx": "0xfoo",
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

        pool = _pool_mock()
        # admin_topup_credit now uses acquire()+transaction(), so mock conn.fetchrow
        pool._conn.fetchrow = AsyncMock(return_value={"balance_usdc": 50_000})
        with patch.object(billing_module, "_pool", pool):
            balance = await admin_topup_credit("org-1", 50_000)
        assert balance == 50_000

    async def test_ledger_insert_called(self):
        """admin_topup_credit must insert a row into org_credit_ledger."""

        pool = _pool_mock()
        pool._conn.fetchrow = AsyncMock(return_value={"balance_usdc": 100_000})
        with patch.object(billing_module, "_pool", pool):
            await admin_topup_credit("org-1", 50_000, reason="manual topup")
        # execute called twice: upsert (via fetchrow) + ledger insert
        pool._conn.execute.assert_called_once()
        call_sql = pool._conn.execute.call_args.args[0]
        assert "org_credit_ledger" in call_sql


@pytest.mark.anyio
class TestDebitCreditMock:
    async def test_debit_returns_false_when_no_row(self):

        pool = _pool_mock()
        pool._conn.fetchrow = AsyncMock(return_value=None)
        with patch.object(billing_module, "_pool", pool):
            result = await debit_credit("org-missing", 5_000)
        assert result is False

    async def test_debit_returns_true_on_success(self):

        pool = _pool_mock()
        pool._conn.fetchrow = AsyncMock(return_value={"balance_usdc": 20_000})
        pool._conn.execute = AsyncMock()
        with patch.object(billing_module, "_pool", pool):
            result = await debit_credit("org-1", 5_000)
        assert result is True

    async def test_debit_inserts_ledger_row(self):
        """debit_credit must insert a row into org_credit_ledger."""

        pool = _pool_mock()
        pool._conn.fetchrow = AsyncMock(return_value={"balance_usdc": 20_000})
        pool._conn.execute = AsyncMock()
        with patch.object(billing_module, "_pool", pool):
            await debit_credit("org-1", 5_000, reason="run:abc")
        assert pool._conn.execute.call_count == 2
        ledger_call_sql = pool._conn.execute.call_args_list[1].args[0]
        assert "org_credit_ledger" in ledger_call_sql

    async def test_debit_returns_false_on_db_exception(self):

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


# ─── _get_server ──────────────────────────────────────────────────────────────


class TestGetServer:
    def test_get_server_raises_when_not_initialised(self):
        with patch.object(billing_module, "_server", None):
            with pytest.raises(RuntimeError, match="Billing not initialised"):
                billing_module._get_server()


# ─── init_billing ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestInitBilling:
    async def test_init_billing_disabled_sets_pool_and_returns(self):
        from billing import init_billing

        mock_settings = MagicMock()
        mock_settings.billing_enabled = False
        mock_pool = MagicMock()

        with (
            patch("billing.get_settings", return_value=mock_settings),
            patch.object(billing_module, "_pool", None),
        ):
            await init_billing(mock_pool)
            assert billing_module._pool is mock_pool


# ─── close_billing ────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestCloseBilling:
    async def test_close_billing_resets_all_state(self):
        from billing import close_billing

        billing_module._server = MagicMock()
        billing_module._pool = MagicMock()
        billing_module._requirements_cache = [MagicMock()]

        await close_billing()

        assert billing_module._server is None
        assert billing_module._pool is None
        assert billing_module._requirements_cache is None


# ─── _get_pool ────────────────────────────────────────────────────────────────


class TestGetPool:
    def test_get_pool_raises_when_uninitialised(self):
        with patch.object(billing_module, "_pool", None):
            with pytest.raises(RuntimeError, match="Billing DB not initialised"):
                billing_module._get_pool()


# ─── get_payment_requirements ─────────────────────────────────────────────────


class TestGetPaymentRequirements:
    def test_raises_when_requirements_cache_is_none(self):
        from billing import get_payment_requirements

        with patch.object(billing_module, "_requirements_cache", None):
            with pytest.raises(RuntimeError, match="Billing not initialised"):
                get_payment_requirements()


# ─── build_402_response_body ──────────────────────────────────────────────────


class TestBuild402ResponseBody:
    def test_returns_expected_shape(self):
        from billing import build_402_response_body

        mock_req = MagicMock()
        mock_req.model_dump.return_value = {"scheme": "exact", "network": "base"}

        with patch.object(billing_module, "_requirements_cache", [mock_req]):
            result = build_402_response_body()

        assert "error" in result
        assert "accepts" in result
        assert result["x402Version"] == 2
        assert len(result["accepts"]) == 1

    def test_dual_requirements_upto_first_exact_second(self):
        """When upto is enabled, 402 body has two accepts: upto first, exact second."""
        from billing import build_402_response_body

        upto_req = MagicMock()
        upto_req.model_dump.return_value = {
            "scheme": "upto",
            "network": "base",
            "amount": "500000",
        }
        exact_req = MagicMock()
        exact_req.model_dump.return_value = {
            "scheme": "exact",
            "network": "base",
            "amount": "10000",
        }

        with patch.object(billing_module, "_requirements_cache", [upto_req, exact_req]):
            result = build_402_response_body()

        assert len(result["accepts"]) == 2
        assert result["accepts"][0]["scheme"] == "upto"
        assert result["accepts"][1]["scheme"] == "exact"


# ─── build_402_headers ────────────────────────────────────────────────────────


class TestBuild402Headers:
    def test_returns_base64_encoded_header(self):
        from billing import build_402_headers

        mock_req = MagicMock()
        mock_req.model_dump.return_value = {"scheme": "exact", "network": "base"}

        with patch.object(billing_module, "_requirements_cache", [mock_req]):
            result = build_402_headers()

        assert "X-PAYMENT-REQUIRED" in result
        assert isinstance(result["X-PAYMENT-REQUIRED"], str)
        assert len(result["X-PAYMENT-REQUIRED"]) > 0


# ─── _rebuild_requirements_if_stale ──────────────────────────────────────────


@pytest.mark.anyio
class TestRebuildRequirementsIfStale:
    async def test_noop_when_server_is_none(self):
        with patch.object(billing_module, "_server", None):
            await billing_module._rebuild_requirements_if_stale()  # should not raise

    async def test_noop_when_no_pricing_rule(self):
        with (
            patch.object(billing_module, "_server", MagicMock()),
            patch("billing.get_live_pricing", new=AsyncMock(return_value=None)),
        ):
            await billing_module._rebuild_requirements_if_stale()  # rule is None → early return


# ─── build_usdc_topup_requirements ────────────────────────────────────────────────────────


class TestBuildUsdcTopupRequirements:
    def test_raises_when_billing_not_initialised(self):
        with patch.object(billing_module, "_server", None):
            with pytest.raises(RuntimeError, match="not initialised"):
                build_usdc_topup_requirements(1_000_000)

    def test_builds_requirements_with_correct_price(self):
        """Server's build_payment_requirements is called with the right price string."""
        mock_server = MagicMock()
        mock_server.build_payment_requirements.return_value = ["req"]

        mock_settings = MagicMock()
        mock_settings.x402_scheme = "exact"
        mock_settings.x402_network = "eip155:84532"
        mock_settings.x402_pay_to_address = "0xTreasury"

        with (
            patch.object(billing_module, "_server", mock_server),
            patch("billing.get_settings", return_value=mock_settings),
        ):
            result = build_usdc_topup_requirements(1_000_000)

        # $1.00 in atomic USDC = 1_000_000 → price string "$1.00"
        call_kwargs = mock_server.build_payment_requirements.call_args[0][0]
        assert call_kwargs.price == "$1.00"
        assert call_kwargs.pay_to == "0xTreasury"
        assert result == ["req"]


# ─── calculate_run_cost_usdc — per-tool override cases ───────────────────────


@pytest.mark.anyio
class TestCalculateRunCostWithOverrides:
    def _usage_rule(self):
        return _make_rule(
            run_price_usdc=0,
            tokens_in_cost_per_1k=1500,
            tokens_out_cost_per_1k=7500,
            tool_call_cost=1000,
        )

    async def test_with_overrides_web_search_billed_at_override(self):
        """web_search should cost 15000, not the global 1000 default."""
        rule = self._usage_rule()
        overrides = {"web_search": 15000}
        with (
            patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)),
            patch("billing.get_tool_pricing_overrides", new=AsyncMock(return_value=overrides)),
        ):
            cost = await calculate_run_cost_usdc({"tokens_in": 0, "tokens_out": 0, "tool_calls": 1, "tool_names": ["web_search"]})
        assert cost == 15_000

    async def test_with_overrides_mixed_tools(self):
        """web_search uses override (15000); calculate uses global default (1000)."""
        rule = self._usage_rule()
        overrides = {"web_search": 15000}
        with (
            patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)),
            patch("billing.get_tool_pricing_overrides", new=AsyncMock(return_value=overrides)),
        ):
            cost = await calculate_run_cost_usdc(
                {
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "tool_calls": 2,
                    "tool_names": ["web_search", "calculate"],
                }
            )
        assert cost == 15_000 + 1_000

    async def test_multi_calls_same_tool(self):
        """Two web_search calls should each cost the override (2 × 15000)."""
        rule = self._usage_rule()
        overrides = {"web_search": 15000}
        with (
            patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)),
            patch("billing.get_tool_pricing_overrides", new=AsyncMock(return_value=overrides)),
        ):
            cost = await calculate_run_cost_usdc(
                {
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "tool_calls": 2,
                    "tool_names": ["web_search", "web_search"],
                }
            )
        assert cost == 2 * 15_000

    async def test_empty_overrides_dict_uses_global_default(self):
        """When overrides is {}, all tools fall back to rule.tool_call_cost."""
        rule = self._usage_rule()
        with (
            patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)),
            patch("billing.get_tool_pricing_overrides", new=AsyncMock(return_value={})),
        ):
            cost = await calculate_run_cost_usdc(
                {
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "tool_calls": 3,
                    "tool_names": ["calculate", "get_datetime", "get_eth_balance"],
                }
            )
        assert cost == 3 * 1_000

    async def test_defensive_fallback_unnamed_extra_calls(self):
        """tool_calls=3 but only 1 name recorded → 1 override + 2 × default."""
        rule = self._usage_rule()
        overrides = {"web_search": 15000}
        with (
            patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)),
            patch("billing.get_tool_pricing_overrides", new=AsyncMock(return_value=overrides)),
        ):
            cost = await calculate_run_cost_usdc(
                {
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "tool_calls": 3,
                    "tool_names": ["web_search"],
                }
            )
        assert cost == 15_000 + 2 * 1_000

    async def test_fallback_to_flat_rate_when_no_per_unit_rates(self):
        """Flat-rate rule: tool_names present but should still return run_price_usdc."""
        rule = _make_rule(run_price_usdc=10_000)
        with (
            patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)),
        ):
            cost = await calculate_run_cost_usdc(
                {
                    "tokens_in": 1000,
                    "tokens_out": 500,
                    "tool_calls": 1,
                    "tool_names": ["web_search"],
                }
            )
        assert cost == 10_000

    async def test_empty_tool_names_list_uses_tool_calls_count(self):
        """When tool_names=[] with tool_calls=2, falls back to tool_calls × default."""
        rule = self._usage_rule()
        with (
            patch("billing.get_live_pricing", new=AsyncMock(return_value=rule)),
        ):
            cost = await calculate_run_cost_usdc({"tokens_in": 0, "tokens_out": 0, "tool_calls": 2, "tool_names": []})
        assert cost == 2 * 1_000

    def test_price_string_for_ten_dollars(self):
        mock_server = MagicMock()
        mock_server.build_payment_requirements.return_value = []
        mock_settings = MagicMock()
        mock_settings.x402_scheme = "exact"
        mock_settings.x402_network = "eip155:84532"
        mock_settings.x402_pay_to_address = "0xTreasury"

        with (
            patch.object(billing_module, "_server", mock_server),
            patch("billing.get_settings", return_value=mock_settings),
        ):
            build_usdc_topup_requirements(10_000_000)  # $10.00

        call_kwargs = mock_server.build_payment_requirements.call_args[0][0]
        assert call_kwargs.price == "$10.00"


# ─── verify_and_settle_usdc_topup ─────────────────────────────────────────────────────


class TestVerifyAndSettleUsdcTopup:
    """Mocks _get_server, build_usdc_topup_requirements, and parse_payment_payload."""

    def _make_server(self, is_valid=True, settle_success=True, tx_hash="0xabc"):
        server = MagicMock()
        verify_result = MagicMock()
        verify_result.is_valid = is_valid
        verify_result.invalid_reason = "bad sig" if not is_valid else None
        verify_result.invalid_message = None
        verify_result.payer = "0xpayer"
        server.verify_payment = AsyncMock(return_value=verify_result)

        settle_result = MagicMock()
        settle_result.success = settle_success
        settle_result.tx_hash = tx_hash
        server.settle_payment = AsyncMock(return_value=settle_result)

        server.build_payment_requirements.return_value = [MagicMock()]
        return server

    async def test_malformed_header_returns_error(self):
        mock_server = self._make_server()
        mock_x402 = MagicMock()
        mock_x402.parse_payment_payload.side_effect = ValueError("bad payload")
        with (
            patch.object(billing_module, "_server", mock_server),
            patch(
                "billing.get_settings",
                return_value=MagicMock(x402_scheme="exact", x402_network="eip155:84532", x402_pay_to_address="0xT"),
            ),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_and_settle_usdc_topup("!notbase64!", 1_000_000)
        assert not result.settled
        assert "Malformed" in result.error

    async def test_verify_failure_returns_error_without_settling(self):
        mock_server = self._make_server(is_valid=False)
        mock_x402 = MagicMock()
        with (
            patch.object(billing_module, "_server", mock_server),
            patch(
                "billing.get_settings",
                return_value=MagicMock(x402_scheme="exact", x402_network="eip155:84532", x402_pay_to_address="0xT"),
            ),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_and_settle_usdc_topup(base64.b64encode(b"dummy").decode(), 1_000_000)
        assert not result.settled
        assert "verification failed" in result.error.lower()
        mock_server.settle_payment.assert_not_called()

    async def test_settle_failure_returns_error(self):
        mock_server = self._make_server(settle_success=False)
        mock_x402 = MagicMock()
        with (
            patch.object(billing_module, "_server", mock_server),
            patch(
                "billing.get_settings",
                return_value=MagicMock(x402_scheme="exact", x402_network="eip155:84532", x402_pay_to_address="0xT"),
            ),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_and_settle_usdc_topup(base64.b64encode(b"dummy").decode(), 1_000_000)
        assert not result.settled
        assert "rejected" in result.error.lower()

    async def test_success_returns_tx_hash_and_amount(self):
        mock_server = self._make_server(tx_hash="0xdeadbeef")
        mock_x402 = MagicMock()
        with (
            patch.object(billing_module, "_server", mock_server),
            patch(
                "billing.get_settings",
                return_value=MagicMock(x402_scheme="exact", x402_network="eip155:84532", x402_pay_to_address="0xT"),
            ),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_and_settle_usdc_topup(base64.b64encode(b"dummy").decode(), 1_000_000)
        assert result.settled
        assert result.tx_hash == "0xdeadbeef"
        assert result.amount_usdc == 1_000_000


# ─── credit_usdc_topup ─────────────────────────────────────────────────────────────────


class TestCreditUsdcTopup:
    """Mocks the DB pool to test idempotency logic and balance accounting."""

    def _make_pool(self, guard_returns_row=True, initial_balance=0):
        """Build a mock pool whose conn.fetchrow / execute return configurable values."""
        conn = MagicMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)

        tx = MagicMock()
        tx.__aenter__ = AsyncMock(return_value=tx)
        tx.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=tx)

        guard_row = MagicMock() if guard_returns_row else None
        credit_row = MagicMock()
        credit_row.__getitem__ = MagicMock(side_effect=lambda k: initial_balance + 1_000_000 if k == "balance_usdc" else None)
        conn.fetchrow = AsyncMock(side_effect=[guard_row, credit_row])
        conn.execute = AsyncMock()

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=conn)
        return pool

    async def test_duplicate_tx_hash_returns_none(self):
        pool = self._make_pool(guard_returns_row=False)
        with patch.object(billing_module, "_pool", pool):
            result = await credit_usdc_topup("org-1", 1_000_000, "0xdupe")
        assert result is None

    async def test_new_topup_returns_new_balance(self):
        pool = self._make_pool(guard_returns_row=True, initial_balance=0)
        with patch.object(billing_module, "_pool", pool):
            result = await credit_usdc_topup("org-1", 1_000_000, "0xnewtx")
        assert result == 1_000_000  # initial 0 + 1_000_000

    async def test_credit_ledger_insert_called(self):
        """Verify a ledger row with operation='topup' and reason 'usdc_onchain:' is inserted."""
        pool = self._make_pool(guard_returns_row=True, initial_balance=0)
        with patch.object(billing_module, "_pool", pool):
            await credit_usdc_topup("org-1", 1_000_000, "0xledgertx")
        conn = pool.acquire.return_value
        # The second execute call is the ledger insert
        execute_calls = conn.execute.call_args_list
        assert len(execute_calls) == 1
        ledger_sql = execute_calls[0][0][0]
        assert "org_credit_ledger" in ledger_sql
        # Reason arg should start with 'usdc_onchain:'
        reason_arg = execute_calls[0][0][5]
        assert reason_arg.startswith("usdc_onchain:")


# ─── upto scheme ──────────────────────────────────────────────────────────────


class TestUptoScheme:
    """Tests for upto-specific billing paths (scheme detection, settlement with actual cost)."""

    async def test_verify_payment_detects_upto_scheme(self):
        """When the matched requirement has scheme='upto', BillingResult.scheme is 'upto'."""
        mock_payload = MagicMock()
        mock_verify = MagicMock(is_valid=True)
        mock_server, mock_req, mock_x402 = _payment_fixture(mock_parse_return=mock_payload)
        mock_req.scheme = "upto"
        mock_server.verify_payment = AsyncMock(return_value=mock_verify)
        with (
            patch.object(billing_module, "_server", mock_server),
            patch.object(billing_module, "_requirements_cache", [mock_req]),
            patch("billing._rebuild_requirements_if_stale", new=AsyncMock()),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_payment(base64.b64encode(b"valid").decode())
        assert result.verified is True
        assert result.scheme == "upto"

    async def test_verify_payment_defaults_to_exact_scheme(self):
        """When the requirement has scheme='exact', BillingResult.scheme is 'exact'."""
        mock_payload = MagicMock()
        mock_verify = MagicMock(is_valid=True)
        mock_server, mock_req, mock_x402 = _payment_fixture(mock_parse_return=mock_payload)
        mock_req.scheme = "exact"
        mock_server.verify_payment = AsyncMock(return_value=mock_verify)
        with (
            patch.object(billing_module, "_server", mock_server),
            patch.object(billing_module, "_requirements_cache", [mock_req]),
            patch("billing._rebuild_requirements_if_stale", new=AsyncMock()),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_payment(base64.b64encode(b"valid").decode())
        assert result.verified is True
        assert result.scheme == "exact"


# ─── TTLCache ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestTTLCache:
    def _make_cache(self, loader, stale_default=None):
        from billing import TTLCache

        return TTLCache(
            name="test",
            redis_key="teardrop:test",
            ttl_seconds_fn=lambda: 60,
            loader=loader,
            serialize=str,
            deserialize=lambda s: s,
            stale_default=stale_default,
        )

    async def test_get_returns_value_from_loader(self):
        cache = self._make_cache(AsyncMock(return_value="hello"))
        with patch("billing.get_redis", return_value=None):
            result = await cache.get()
        assert result == "hello"

    async def test_in_process_cache_hit_skips_loader(self):
        import time

        loader = AsyncMock(return_value="first")
        cache = self._make_cache(loader)
        cache._value = "cached"
        cache._expires = time.monotonic() + 9999
        with patch("billing.get_redis", return_value=None):
            result = await cache.get()
        assert result == "cached"
        loader.assert_not_called()

    async def test_loader_failure_returns_stale_default(self):
        loader = AsyncMock(side_effect=RuntimeError("DB down"))
        cache = self._make_cache(loader, stale_default="fallback")
        with patch("billing.get_redis", return_value=None):
            result = await cache.get()
        assert result == "fallback"

    async def test_loader_failure_returns_stale_value(self):
        loader = AsyncMock(side_effect=RuntimeError("DB down"))
        cache = self._make_cache(loader)
        cache._value = "stale"
        cache._expires = 0.0  # expired in-process, but stale present
        with patch("billing.get_redis", return_value=None):
            result = await cache.get()
        assert result == "stale"

    async def test_get_redis_path(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value='"from_redis"')
        cache = self._make_cache(AsyncMock(return_value="from_loader"))
        cache._deserialize = lambda s: s.strip('"')
        with patch("billing.get_redis", return_value=redis):
            result = await cache.get()
        assert result == "from_redis"

    async def test_invalidate_clears_value(self):
        cache = self._make_cache(AsyncMock(return_value="v"))
        cache._value = "old"
        with patch("billing.get_redis", return_value=None):
            await cache.invalidate()
        assert cache._value is None
        assert cache._expires == 0.0

    def test_reset_clears_in_process_tier(self):
        cache = self._make_cache(AsyncMock())
        cache._value = "v"
        cache._expires = 999.0
        cache.reset()
        assert cache._value is None
        assert cache._expires == 0.0


# ─── upsert / delete tool pricing overrides ──────────────────────────────────


@pytest.mark.anyio
class TestToolPricingOverrides:
    async def test_upsert_executes_upsert(self):
        from billing import upsert_tool_pricing_override

        pool = MagicMock()
        pool.execute = AsyncMock()
        with patch.object(billing_module, "_pool", pool):
            with patch.object(billing_module._tool_overrides_cache_obj, "invalidate", AsyncMock()):
                await upsert_tool_pricing_override("my_tool", 5000, "desc")
        pool.execute.assert_called_once()

    async def test_delete_returns_true_when_deleted(self):
        from billing import delete_tool_pricing_override

        pool = MagicMock()
        pool.execute = AsyncMock(return_value="DELETE 1")
        with patch.object(billing_module, "_pool", pool):
            with patch.object(billing_module._tool_overrides_cache_obj, "invalidate", AsyncMock()):
                result = await delete_tool_pricing_override("my_tool")
        assert result is True

    async def test_delete_returns_false_when_not_found(self):
        from billing import delete_tool_pricing_override

        pool = MagicMock()
        pool.execute = AsyncMock(return_value="DELETE 0")
        with patch.object(billing_module, "_pool", pool):
            with patch.object(billing_module._tool_overrides_cache_obj, "invalidate", AsyncMock()):
                result = await delete_tool_pricing_override("missing_tool")
        assert result is False


# ─── get_live_pricing_for_model ───────────────────────────────────────────────


@pytest.mark.anyio
class TestGetLivePricingForModel:
    async def test_no_provider_falls_back_to_global(self):
        from billing import get_live_pricing_for_model

        mock_rule = _make_rule()
        with patch("billing.get_live_pricing", AsyncMock(return_value=mock_rule)):
            with patch("billing.get_redis", return_value=None):
                result = await get_live_pricing_for_model("", "")
        assert result is mock_rule

    async def test_fetches_from_db_and_caches(self):
        from billing import get_live_pricing_for_model

        mock_rule = _make_rule()
        billing_module._model_pricing_cache.pop("openai:gpt-4:False", None)
        with patch.object(billing_module, "_pool", MagicMock()):
            with patch("billing.get_redis", return_value=None):
                with patch("billing.get_current_pricing_for_model", AsyncMock(return_value=mock_rule)):
                    result = await get_live_pricing_for_model("openai", "gpt-4")
        assert result is mock_rule
        billing_module._model_pricing_cache.pop("openai:gpt-4:False", None)

    async def test_returns_none_when_pool_is_none(self):
        from billing import get_live_pricing_for_model

        billing_module._model_pricing_cache.pop("openai:gpt-4:False", None)
        with patch.object(billing_module, "_pool", None):
            with patch("billing.get_redis", return_value=None):
                result = await get_live_pricing_for_model("openai", "gpt-4")
        assert result is None


# ─── get_revenue_summary ──────────────────────────────────────────────────────


@pytest.mark.anyio
class TestGetRevenueSummary:
    async def test_returns_row_as_dict(self):
        from billing import get_revenue_summary

        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value={"total_settlements": 5, "total_revenue_usdc": 50000})
        with patch.object(billing_module, "_pool", pool):
            result = await get_revenue_summary()
        assert result["total_settlements"] == 5
        assert result["total_revenue_usdc"] == 50000

    async def test_returns_zeros_when_no_row(self):
        from billing import get_revenue_summary

        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(billing_module, "_pool", pool):
            result = await get_revenue_summary()
        assert result == {"total_settlements": 0, "total_revenue_usdc": 0}

    async def test_applies_date_range_filters(self):
        from datetime import datetime, timezone

        from billing import get_revenue_summary

        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value={"total_settlements": 1, "total_revenue_usdc": 1000})
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 12, 31, tzinfo=timezone.utc)
        with patch.object(billing_module, "_pool", pool):
            result = await get_revenue_summary(start=start, end=end)
        assert result["total_settlements"] == 1
        # Verify params were passed
        call_args = pool.fetchrow.call_args
        assert start in call_args[0]
        assert end in call_args[0]

    async def test_settle_upto_passes_actual_amount(self):
        """Upto settlement clones requirements with actual cost in atomic units.

        x402ResourceServer.settle_payment() has no actual_amount kwarg.  The correct
        pattern is model_copy(update={"amount": str(actual_cost_usdc)}) then pass the
        cloned requirements as a positional argument.
        """
        mock_settle = MagicMock(success=True, tx_hash="0xuptotx", transaction_hash=None)
        mock_server = MagicMock()
        mock_server.settle_payment = AsyncMock(return_value=mock_settle)
        mock_req = MagicMock(amount="500000")
        verified = BillingResult(
            verified=True,
            payment_payload=MagicMock(),
            payment_requirements=mock_req,
            scheme="upto",
        )
        actual_cost = 150_000  # $0.15 actual usage
        with patch.object(billing_module, "_server", mock_server):
            result = await settle_payment(verified, actual_cost_usdc=actual_cost)
        assert result.settled is True
        assert result.tx_hash == "0xuptotx"
        assert result.amount_usdc == actual_cost
        # requirements.model_copy called with atomic units as string, NOT
        # a dollar string.
        mock_req.model_copy.assert_called_once_with(
            update={"amount": "150000"},
        )
        # settle_payment receives the cloned requirements as a positional
        # arg; no actual_amount kwarg.
        call_args = mock_server.settle_payment.call_args
        assert call_args.args[1] is mock_req.model_copy.return_value
        assert "actual_amount" not in (call_args.kwargs or {})

    async def test_settle_exact_ignores_actual_cost(self):
        """Exact settlement does NOT pass actual_amount even if provided."""
        mock_settle = MagicMock(success=True, tx_hash="0xexacttx", transaction_hash=None)
        mock_server = MagicMock()
        mock_server.settle_payment = AsyncMock(return_value=mock_settle)
        verified = BillingResult(
            verified=True,
            payment_payload=MagicMock(),
            payment_requirements=MagicMock(amount="10000"),
            scheme="exact",
        )
        with patch.object(billing_module, "_server", mock_server):
            result = await settle_payment(verified, actual_cost_usdc=5_000)
        assert result.settled is True
        # Exact scheme uses the requirement amount, not actual_cost_usdc.
        assert result.amount_usdc == 10_000
        # Verify actual_amount was NOT passed
        call_kwargs = mock_server.settle_payment.call_args
        assert "actual_amount" not in (call_kwargs.kwargs or {})

    async def test_settle_upto_without_actual_cost_uses_requirement(self):
        """Upto settlement without actual_cost_usdc falls back to requirement amount."""
        mock_settle = MagicMock(success=True, tx_hash="0xfallback", transaction_hash=None)
        mock_server = MagicMock()
        mock_server.settle_payment = AsyncMock(return_value=mock_settle)
        verified = BillingResult(
            verified=True,
            payment_payload=MagicMock(),
            payment_requirements=MagicMock(amount="500000"),
            scheme="upto",
        )
        with patch.object(billing_module, "_server", mock_server):
            result = await settle_payment(verified)  # no actual_cost_usdc
        assert result.settled is True
        # Falls back to requirement amount path (no actual_amount sent)
        assert result.amount_usdc == 500_000

    async def test_settle_upto_zero_cost_clones_with_zero_atomic(self):
        """Zero-cost upto settlement sets amount='0' (not '$0.00') in cloned requirements."""
        mock_settle = MagicMock(success=True, tx_hash="0xzerotx", transaction_hash=None)
        mock_server = MagicMock()
        mock_server.settle_payment = AsyncMock(return_value=mock_settle)
        mock_req = MagicMock(amount="500000")
        verified = BillingResult(
            verified=True,
            payment_payload=MagicMock(),
            payment_requirements=mock_req,
            scheme="upto",
        )
        with patch.object(billing_module, "_server", mock_server):
            result = await settle_payment(verified, actual_cost_usdc=0)
        assert result.settled is True
        assert result.amount_usdc == 0
        mock_req.model_copy.assert_called_once_with(update={"amount": "0"})
        call_args = mock_server.settle_payment.call_args
        assert call_args.args[1] is mock_req.model_copy.return_value

    def test_billing_result_scheme_defaults_to_exact(self):
        """BillingResult.scheme defaults to 'exact'."""
        br = BillingResult()
        assert br.scheme == "exact"

    # ─── Multi-requirement iteration ──────────────────────────────────────

    async def test_verify_iterates_reqs_exact_payload_falls_through_to_exact(self):
        """When reqs=[upto, exact] and upto fails, exact requirement succeeds."""
        mock_payload = MagicMock()
        mock_x402 = MagicMock()
        mock_x402.parse_payment_payload.return_value = mock_payload

        upto_req = MagicMock()
        upto_req.scheme = "upto"
        exact_req = MagicMock()
        exact_req.scheme = "exact"

        # upto verify fails, exact verify succeeds
        upto_fail = MagicMock(
            is_valid=False,
            invalid_reason="wrong scheme",
            payer="0xabc",
            invalid_message=None,
        )
        exact_pass = MagicMock(is_valid=True)

        mock_server = MagicMock()
        mock_server.verify_payment = AsyncMock(side_effect=[upto_fail, exact_pass])

        with (
            patch.object(billing_module, "_server", mock_server),
            patch.object(billing_module, "_requirements_cache", [upto_req, exact_req]),
            patch("billing._rebuild_requirements_if_stale", new=AsyncMock()),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_payment(base64.b64encode(b"exact-signed").decode())

        assert result.verified is True
        assert result.scheme == "exact"
        assert result.payment_requirements is exact_req
        assert mock_server.verify_payment.call_count == 2

    async def test_verify_iterates_reqs_upto_matches_first(self):
        """When reqs=[upto, exact] and upto succeeds, exact is never tried."""
        mock_payload = MagicMock()
        mock_x402 = MagicMock()
        mock_x402.parse_payment_payload.return_value = mock_payload

        upto_req = MagicMock()
        upto_req.scheme = "upto"
        exact_req = MagicMock()
        exact_req.scheme = "exact"

        upto_pass = MagicMock(is_valid=True)
        mock_server = MagicMock()
        mock_server.verify_payment = AsyncMock(return_value=upto_pass)

        with (
            patch.object(billing_module, "_server", mock_server),
            patch.object(billing_module, "_requirements_cache", [upto_req, exact_req]),
            patch("billing._rebuild_requirements_if_stale", new=AsyncMock()),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_payment(base64.b64encode(b"upto-signed").decode())

        assert result.verified is True
        assert result.scheme == "upto"
        # Only called once — exact was never tried
        assert mock_server.verify_payment.call_count == 1

    async def test_verify_iterates_reqs_all_fail_returns_last_error(self):
        """When all requirements fail, returns the last error message."""
        mock_payload = MagicMock()
        mock_x402 = MagicMock()
        mock_x402.parse_payment_payload.return_value = mock_payload

        upto_req = MagicMock()
        upto_req.scheme = "upto"
        exact_req = MagicMock()
        exact_req.scheme = "exact"

        upto_fail = MagicMock(
            is_valid=False,
            invalid_reason="bad permit2 sig",
            payer="0xabc",
            invalid_message=None,
        )
        exact_fail = MagicMock(
            is_valid=False,
            invalid_reason="wrong amount",
            payer="0xabc",
            invalid_message=None,
        )

        mock_server = MagicMock()
        mock_server.verify_payment = AsyncMock(side_effect=[upto_fail, exact_fail])

        with (
            patch.object(billing_module, "_server", mock_server),
            patch.object(billing_module, "_requirements_cache", [upto_req, exact_req]),
            patch("billing._rebuild_requirements_if_stale", new=AsyncMock()),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_payment(base64.b64encode(b"bad-payload").decode())

        assert result.verified is False
        assert "wrong amount" in result.error

    async def test_verify_iterates_exception_continues_to_next(self):
        """When one requirement throws an exception, the next is still tried."""
        mock_payload = MagicMock()
        mock_x402 = MagicMock()
        mock_x402.parse_payment_payload.return_value = mock_payload

        upto_req = MagicMock()
        upto_req.scheme = "upto"
        exact_req = MagicMock()
        exact_req.scheme = "exact"

        exact_pass = MagicMock(is_valid=True)
        mock_server = MagicMock()
        mock_server.verify_payment = AsyncMock(side_effect=[Exception("permit2 decode error"), exact_pass])

        with (
            patch.object(billing_module, "_server", mock_server),
            patch.object(billing_module, "_requirements_cache", [upto_req, exact_req]),
            patch("billing._rebuild_requirements_if_stale", new=AsyncMock()),
            patch.dict("sys.modules", {"x402": mock_x402}),
        ):
            result = await verify_payment(base64.b64encode(b"exact-signed").decode())

        assert result.verified is True
        assert result.scheme == "exact"

    # ─── Negative actual_cost_usdc guard ──────────────────────────────────

    async def test_settle_upto_negative_cost_floors_to_zero(self):
        """Negative actual_cost_usdc is floored to 0 to prevent contract revert."""
        mock_settle = MagicMock(success=True, tx_hash="0xguardtx", transaction_hash=None)
        mock_server = MagicMock()
        mock_server.settle_payment = AsyncMock(return_value=mock_settle)
        mock_req = MagicMock(amount="500000")
        verified = BillingResult(
            verified=True,
            payment_payload=MagicMock(),
            payment_requirements=mock_req,
            scheme="upto",
        )
        with patch.object(billing_module, "_server", mock_server):
            result = await settle_payment(verified, actual_cost_usdc=-100)
        assert result.settled is True
        assert result.amount_usdc == 0
        mock_req.model_copy.assert_called_once_with(update={"amount": "0"})
