# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Stripe embedded checkout and webhook processing for credit top-up."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sentry_sdk

from billing.context import _get_daily_debit_spend, _get_daily_spend_cache, _get_pool
from billing.credit import BillingCreditService
from billing.models import BillingResult, atomic_usdc_to_price_str
from teardrop.config import get_settings

logger = logging.getLogger(__name__)


def _get_credit_service() -> BillingCreditService:
    return BillingCreditService(
        get_pool=_get_pool,
        get_daily_spend_cache=_get_daily_spend_cache,
        get_daily_debit_spend_fn=_get_daily_debit_spend,
        billing_result_factory=BillingResult,
    )


async def create_stripe_embedded_session(org_id: str, user_id: str, amount_cents: int, return_url: str) -> dict[str, str]:
    """Create a Stripe Checkout session for embedded checkout."""
    import stripe  # noqa: PLC0415

    settings = get_settings()
    if not settings.stripe_secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = settings.stripe_secret_key

    amount_usdc = amount_cents * 10_000
    session = await stripe.checkout.Session.create_async(
        mode="payment",
        ui_mode="embedded_page",
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Teardrop Cloud Credits"},
                    "unit_amount": amount_cents,
                },
                "quantity": 1,
            }
        ],
        client_reference_id=org_id,
        metadata={
            "org_id": org_id,
            "user_id": user_id,
            "amount_usdc": str(amount_usdc),
        },
        return_url=return_url,
    )
    if not session.client_secret:
        raise RuntimeError(f"Stripe returned a session without client_secret (session_id={session.id})")
    return {"client_secret": session.client_secret, "session_id": session.id}


async def handle_stripe_webhook(payload: bytes, sig_header: str) -> None:
    """Verify and process a Stripe webhook event."""
    import stripe  # noqa: PLC0415

    settings = get_settings()

    if not sig_header:
        raise ValueError("Missing Stripe-Signature header")

    event = stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)

    if event.type != "checkout.session.completed":
        return

    session = event.data.object
    if session.payment_status != "paid":
        return

    org_id: str | None = session.client_reference_id or (session.metadata.get("org_id") if session.metadata else None)
    if not org_id:
        logger.error("stripe webhook: no org_id in event %s", event.id)
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("webhook_event_id", str(event.id))
            scope.set_tag("rail", "stripe")
            scope.set_tag("reason", "missing_org_id")
            sentry_sdk.capture_message(
                "stripe webhook: no org_id in event",
                level="error",
            )
        return

    raw_meta = (session.metadata or {}).get("amount_usdc")
    try:
        amount_usdc = int(raw_meta) if raw_meta else int(session.amount_total or 0) * 10_000
    except (ValueError, TypeError):
        logger.warning(
            "stripe webhook: bad amount_usdc metadata %r for event %s — using amount_total",
            raw_meta,
            event.id,
        )
        amount_usdc = int(session.amount_total or 0) * 10_000

    if amount_usdc <= 0:
        logger.error("stripe webhook: non-positive amount_usdc=%s event %s", amount_usdc, event.id)
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("webhook_event_id", str(event.id))
            scope.set_tag("rail", "stripe")
            scope.set_tag("reason", "non_positive_amount")
            scope.set_tag("amount_usdc_atomic", str(amount_usdc))
            sentry_sdk.capture_message(
                "stripe webhook: non-positive amount",
                level="error",
            )
        return

    pool = _get_pool()
    org_exists = await pool.fetchval("SELECT EXISTS(SELECT 1 FROM orgs WHERE id = $1)", org_id)
    if not org_exists:
        logger.error("stripe webhook: unknown org_id=%s event=%s — rejecting", org_id, event.id)
        raise ValueError(f"unknown org_id {org_id!r}")

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '8000'")
                row = await conn.fetchrow(
                    """
                    INSERT INTO stripe_webhook_events (stripe_event_id, org_id, amount_usdc)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (stripe_event_id) DO NOTHING
                    RETURNING stripe_event_id
                    """,
                    event.id,
                    org_id,
                    amount_usdc,
                )
                if row is None:
                    logger.info("stripe webhook: duplicate event %s ignored", event.id)
                    return

                credit_row = await conn.fetchrow(
                    """
                    INSERT INTO org_credits (org_id, balance_usdc, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (org_id) DO UPDATE
                        SET balance_usdc = org_credits.balance_usdc + EXCLUDED.balance_usdc,
                            updated_at = NOW()
                    RETURNING balance_usdc
                    """,
                    org_id,
                    amount_usdc,
                )
                new_balance = int(credit_row["balance_usdc"])
                await conn.execute(
                    """
                    INSERT INTO org_credit_ledger
                        (id, org_id, operation, amount_usdc, balance_usdc_after, reason, created_at)
                    VALUES ($1, $2, 'topup', $3, $4, $5, NOW())
                    """,
                    str(uuid.uuid4()),
                    org_id,
                    amount_usdc,
                    new_balance,
                    f"stripe:{event.id}",
                )
    except Exception as exc:
        logger.exception(
            "stripe webhook: DB error processing event=%s org_id=%s amount_usdc=%s",
            event.id,
            org_id,
            amount_usdc,
        )
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("webhook_event_id", str(event.id))
            scope.set_tag("webhook_type", str(event.type))
            scope.set_tag("org_id", str(org_id))
            scope.set_tag("amount_usdc_atomic", str(amount_usdc))
            scope.set_tag("rail", "stripe")
            sentry_sdk.capture_exception(exc)
        raise

    logger.info(
        "stripe webhook: topped up org_id=%s amount_usdc=%s event=%s",
        org_id,
        amount_usdc,
        event.id,
    )


async def get_stripe_session_status(session_id: str, org_id: str) -> dict[str, Any]:
    """Retrieve a Stripe Checkout session's status and optional updated balance."""
    import stripe  # noqa: PLC0415

    settings = get_settings()
    if not settings.stripe_secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = settings.stripe_secret_key

    session = await stripe.checkout.Session.retrieve_async(session_id)

    if session.client_reference_id != org_id:
        raise PermissionError(f"Session {session_id} does not belong to org_id {org_id}")

    result: dict[str, Any] = {"status": session.status}

    if session.status == "complete":
        balance_usdc = await _get_credit_service().get_credit_balance(org_id)
        result["new_balance_fmt"] = atomic_usdc_to_price_str(balance_usdc)

    return result
