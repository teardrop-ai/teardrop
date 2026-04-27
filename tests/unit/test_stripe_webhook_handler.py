"""Unit tests for billing.handle_stripe_webhook.

All external interactions (Stripe SDK, DB pool) are mocked so this suite runs
without live Postgres or real Stripe credentials.  Follows the _pool_mock /
_async_ctx pattern established in tests/unit/test_billing.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import stripe

import billing as billing_module
from billing import handle_stripe_webhook

# ─── Helpers ──────────────────────────────────────────────────────────────────


class _async_ctx:
    """Minimal async context manager that yields *value* from __aenter__."""

    def __init__(self, value=None):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        pass


_SENTINEL = object()


def _pool_mock_for_webhook(*, event_row=_SENTINEL, credit_row=None):
    """Build a billing._pool mock pre-wired for handle_stripe_webhook DB calls.

    event_row: return value for the first fetchrow (stripe_webhook_events INSERT).
               Defaults to a row dict indicating a new (non-duplicate) event.
               Pass None to simulate a duplicate (ON CONFLICT DO NOTHING → no row).
    credit_row: return value for the second fetchrow (org_credits upsert).
    """
    if event_row is _SENTINEL:
        event_row = {"stripe_event_id": "evt_test_001"}
    if credit_row is None:
        credit_row = {"balance_usdc": 100_000}

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[event_row, credit_row])
    conn.execute = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=_async_ctx(conn))

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    pool._conn = conn  # convenience for assertions
    return pool


def _make_stripe_event(
    event_type: str = "checkout.session.completed",
    payment_status: str = "paid",
    client_reference_id: str = "org-test-1",
    amount_total: int = 5_000,  # $50.00 in USD cents
    metadata: dict | None = None,
    event_id: str = "evt_test_001",
) -> MagicMock:
    """Return a MagicMock shaped like a Stripe Event for checkout.session.completed."""
    if metadata is None:
        metadata = {"org_id": "org-test-1", "amount_usdc": "50000000"}

    session = MagicMock()
    session.payment_status = payment_status
    session.client_reference_id = client_reference_id
    session.amount_total = amount_total
    session.metadata = metadata  # real dict so .get() works

    event = MagicMock()
    event.type = event_type
    event.id = event_id
    event.data.object = session
    return event


VALID_PAYLOAD = b'{"type":"checkout.session.completed"}'
VALID_SIG = "t=1,v1=abc"


# ─── Signature / Validation ───────────────────────────────────────────────────


@pytest.mark.anyio
class TestHandleStripeWebhookSignature:
    async def test_empty_sig_header_raises_ValueError(self):
        """Missing Stripe-Signature header raises ValueError before SDK call."""
        with patch("stripe.Webhook.construct_event") as mock_construct:
            with pytest.raises(ValueError, match="Missing Stripe-Signature header"):
                await handle_stripe_webhook(VALID_PAYLOAD, "")
            mock_construct.assert_not_called()

    async def test_invalid_signature_raises_SignatureVerificationError(self):
        """Bad signature propagates stripe.SignatureVerificationError to caller."""
        with patch("stripe.Webhook.construct_event") as mock_construct:
            mock_construct.side_effect = stripe.SignatureVerificationError("No signatures found", VALID_SIG)
            with pytest.raises(stripe.SignatureVerificationError):
                await handle_stripe_webhook(VALID_PAYLOAD, VALID_SIG)

    async def test_invalid_json_raises_ValueError(self):
        """Malformed JSON body raises ValueError to caller."""
        with patch("stripe.Webhook.construct_event") as mock_construct:
            mock_construct.side_effect = ValueError("No JSON body")
            with pytest.raises(ValueError):
                await handle_stripe_webhook(b"not-json", VALID_SIG)


# ─── Event Filtering ──────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestHandleStripeWebhookEventFiltering:
    async def test_non_checkout_event_skips_db(self):
        """Events other than checkout.session.completed are ignored without DB call."""
        event = _make_stripe_event(event_type="payment_intent.succeeded")
        pool = _pool_mock_for_webhook()
        with (
            patch("stripe.Webhook.construct_event", return_value=event),
            patch.object(billing_module, "_pool", pool),
        ):
            await handle_stripe_webhook(VALID_PAYLOAD, VALID_SIG)
        pool._conn.fetchrow.assert_not_called()

    async def test_payment_status_not_paid_skips_db(self):
        """Sessions with payment_status != 'paid' are ignored without DB call."""
        event = _make_stripe_event(payment_status="unpaid")
        pool = _pool_mock_for_webhook()
        with (
            patch("stripe.Webhook.construct_event", return_value=event),
            patch.object(billing_module, "_pool", pool),
        ):
            await handle_stripe_webhook(VALID_PAYLOAD, VALID_SIG)
        pool._conn.fetchrow.assert_not_called()

    async def test_missing_org_id_skips_db(self):
        """Event with no org_id in client_reference_id or metadata logs error and returns."""
        event = _make_stripe_event(
            client_reference_id=None,
            metadata={},  # no org_id key
        )
        # MagicMock returns a MagicMock for client_reference_id unless explicitly set
        event.data.object.client_reference_id = None
        pool = _pool_mock_for_webhook()
        with (
            patch("stripe.Webhook.construct_event", return_value=event),
            patch.object(billing_module, "_pool", pool),
        ):
            await handle_stripe_webhook(VALID_PAYLOAD, VALID_SIG)
        pool._conn.fetchrow.assert_not_called()


# ─── Amount Parsing ───────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestHandleStripeWebhookAmountParsing:
    async def test_metadata_amount_preferred_over_amount_total(self):
        """amount_usdc from metadata is used when present and valid."""
        event = _make_stripe_event(
            metadata={"org_id": "org-test-1", "amount_usdc": "50000000"},
            amount_total=9999,  # should be ignored
        )
        pool = _pool_mock_for_webhook()
        with (
            patch("stripe.Webhook.construct_event", return_value=event),
            patch.object(billing_module, "_pool", pool),
        ):
            await handle_stripe_webhook(VALID_PAYLOAD, VALID_SIG)
        # First fetchrow arg at index 2 is amount_usdc
        call_args = pool._conn.fetchrow.call_args_list[0].args
        assert 50_000_000 in call_args

    async def test_malformed_metadata_falls_back_to_amount_total(self):
        """Non-integer amount_usdc metadata falls back to amount_total × 10_000."""
        event = _make_stripe_event(
            metadata={"org_id": "org-test-1", "amount_usdc": "not-a-number"},
            amount_total=5_000,  # $50.00 → 50_000_000 atomic USDC
        )
        pool = _pool_mock_for_webhook()
        with (
            patch("stripe.Webhook.construct_event", return_value=event),
            patch.object(billing_module, "_pool", pool),
        ):
            await handle_stripe_webhook(VALID_PAYLOAD, VALID_SIG)
        call_args = pool._conn.fetchrow.call_args_list[0].args
        assert 50_000_000 in call_args

    async def test_zero_amount_skips_db(self):
        """amount_usdc of 0 logs error and returns without DB call."""
        event = _make_stripe_event(
            metadata={"org_id": "org-test-1", "amount_usdc": "0"},
        )
        pool = _pool_mock_for_webhook()
        with (
            patch("stripe.Webhook.construct_event", return_value=event),
            patch.object(billing_module, "_pool", pool),
        ):
            await handle_stripe_webhook(VALID_PAYLOAD, VALID_SIG)
        pool._conn.fetchrow.assert_not_called()

    async def test_negative_amount_skips_db(self):
        """Negative amount_usdc logs error and returns without DB call."""
        event = _make_stripe_event(
            metadata={"org_id": "org-test-1", "amount_usdc": "-1000"},
        )
        pool = _pool_mock_for_webhook()
        with (
            patch("stripe.Webhook.construct_event", return_value=event),
            patch.object(billing_module, "_pool", pool),
        ):
            await handle_stripe_webhook(VALID_PAYLOAD, VALID_SIG)
        pool._conn.fetchrow.assert_not_called()


# ─── DB Operations ────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestHandleStripeWebhookDbOperations:
    async def test_successful_topup_inserts_event_credit_and_ledger(self):
        """Happy path: inserts stripe event, updates credits, and writes ledger row."""
        event = _make_stripe_event(
            metadata={"org_id": "org-test-1", "amount_usdc": "50000000"},
        )
        pool = _pool_mock_for_webhook(
            event_row={"stripe_event_id": "evt_test_001"},
            credit_row={"balance_usdc": 150_000_000},
        )
        with (
            patch("stripe.Webhook.construct_event", return_value=event),
            patch.object(billing_module, "_pool", pool),
        ):
            await handle_stripe_webhook(VALID_PAYLOAD, VALID_SIG)

        assert pool._conn.fetchrow.call_count == 2
        # Idempotency insert
        first_sql = pool._conn.fetchrow.call_args_list[0].args[0]
        assert "stripe_webhook_events" in first_sql
        # Credit upsert
        second_sql = pool._conn.fetchrow.call_args_list[1].args[0]
        assert "org_credits" in second_sql
        # Ledger insert
        pool._conn.execute.assert_called_once()
        ledger_sql = pool._conn.execute.call_args.args[0]
        assert "org_credit_ledger" in ledger_sql

    async def test_duplicate_event_id_is_silently_ignored(self):
        """ON CONFLICT DO NOTHING → fetchrow returns None → function returns early."""
        event = _make_stripe_event()
        pool = _pool_mock_for_webhook(event_row=None)  # duplicate → no row returned
        with (
            patch("stripe.Webhook.construct_event", return_value=event),
            patch.object(billing_module, "_pool", pool),
        ):
            await handle_stripe_webhook(VALID_PAYLOAD, VALID_SIG)

        # Only the idempotency INSERT is attempted; credit upsert and ledger are skipped
        assert pool._conn.fetchrow.call_count == 1
        pool._conn.execute.assert_not_called()

    async def test_db_error_propagates_for_stripe_retry(self):
        """DB failure during transaction is logged and re-raised so Stripe retries (500)."""
        event = _make_stripe_event()
        pool = MagicMock()
        pool.acquire = MagicMock(side_effect=RuntimeError("DB connection lost"))
        with (
            patch("stripe.Webhook.construct_event", return_value=event),
            patch.object(billing_module, "_pool", pool),
        ):
            with pytest.raises(RuntimeError, match="DB connection lost"):
                await handle_stripe_webhook(VALID_PAYLOAD, VALID_SIG)
