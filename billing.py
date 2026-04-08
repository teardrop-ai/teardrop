# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""x402 billing layer for Teardrop.

Provides server-side x402 payment verification and settlement for
the /agent/run SSE endpoint.

Two complementary systems:
- Dynamic pricing: payment requirements rebuilt from pricing_rules DB table
  on a TTL cache (default 5 min) instead of a hardcoded config value.
- Usage-based cost accounting: calculate_run_cost_usdc() computes the true
  cost of a run from token + tool consumption and stores it in usage_events.
  This is the internal accounting layer that maps to the x402 'upto' scheme
  once that scheme lands in the Python library.

Manual wiring (not middleware) because SSE streaming is incompatible with
the standard request/response middleware pattern — we must verify before
streaming begins and settle after the stream completes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from pydantic import BaseModel, Field

from cache import get_redis
from config import get_settings

logger = logging.getLogger(__name__)

# ─── Lazy x402 imports (only when billing is enabled) ─────────────────────────

_server = None  # x402ResourceServer instance
_requirements_cache: list | None = None  # cached PaymentRequirements for /agent/run

# ─── Pricing TTL cache ────────────────────────────────────────────────────────

_pricing_cache: PricingRule | None = None  # type: ignore[name-defined]  # resolved at runtime
_pricing_cache_expires: float = 0.0  # monotonic clock deadline
_pricing_lock: asyncio.Lock | None = None  # lazily initialised
_last_requirements_price_usdc: int = -1  # run_price_usdc when requirements were last built


def _get_server():
    """Return the initialized x402ResourceServer, or raise if not ready."""
    if _server is None:
        raise RuntimeError("Billing not initialised — call init_billing() first")
    return _server


# ─── Initialisation ──────────────────────────────────────────────────────────


async def init_billing(pool: asyncpg.Pool) -> None:
    """Initialise x402 resource server and cache payment requirements.

    Always stores the DB pool so that pricing queries and cost accounting work
    even when billing_enabled=False.
    Call during app lifespan startup.
    """
    global _server, _requirements_cache, _pool, _last_requirements_price_usdc

    settings = get_settings()

    # Always store pool — pricing queries run regardless of billing_enabled.
    _pool = pool

    if not settings.billing_enabled:
        logger.info("Billing disabled — skipping x402 initialisation")
        return

    if not settings.x402_pay_to_address:
        raise RuntimeError("billing_enabled=True but x402_pay_to_address is empty")

    from x402.http import FacilitatorConfig, HTTPFacilitatorClient
    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.schemas import ResourceConfig
    from x402.server import x402ResourceServer

    facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=settings.x402_facilitator_url))
    server = x402ResourceServer(facilitator)
    server.register(settings.x402_network, ExactEvmServerScheme())
    server.initialize()
    _server = server

    # Resolve price from live pricing_rules; fall back to config value.
    rule = await get_live_pricing()
    if rule is not None:
        price_str = atomic_usdc_to_price_str(rule.run_price_usdc)
        _last_requirements_price_usdc = rule.run_price_usdc
    else:
        price_str = settings.x402_run_price
        logger.warning("No pricing_rules row found; using config fallback price=%s", price_str)

    config = ResourceConfig(
        scheme=settings.x402_scheme,
        network=settings.x402_network,
        pay_to=settings.x402_pay_to_address,
        price=price_str,
    )
    _requirements_cache = server.build_payment_requirements(config)

    logger.info(
        "Billing initialised: network=%s pay_to=%s price=%s scheme=%s",
        settings.x402_network,
        settings.x402_pay_to_address,
        price_str,
        settings.x402_scheme,
    )


async def close_billing() -> None:
    """Release billing resources."""
    global _server, _requirements_cache, _pool
    global _pricing_cache, _pricing_cache_expires, _last_requirements_price_usdc
    _server = None
    _requirements_cache = None
    _pool = None
    _pricing_cache = None
    _pricing_cache_expires = 0.0
    _last_requirements_price_usdc = -1
    logger.info("Billing resources released")


# ─── Database pool ────────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Billing DB not initialised")
    return _pool


# ─── Models ───────────────────────────────────────────────────────────────────


class PricingRule(BaseModel):
    id: str
    name: str
    run_price_usdc: int  # atomic units (6 decimals), e.g. 10000 = $0.01
    tokens_in_cost_per_1k: int = 0
    tokens_out_cost_per_1k: int = 0
    tool_call_cost: int = 0
    effective_from: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BillingResult(BaseModel):
    """Result of a verify or settle operation, carried through the SSE stream."""

    verified: bool = False
    payment_payload: object | None = None
    payment_requirements: object | None = None
    settled: bool = False
    tx_hash: str = ""
    amount_usdc: int = 0
    error: str = ""
    # Distinguishes on-chain x402 settlement from off-chain credit debit.
    # Set by verify_payment ("x402") or verify_credit ("credit").
    billing_method: str = "x402"


# ─── Price conversion ────────────────────────────────────────────────────────


def atomic_usdc_to_price_str(atomic: int) -> str:
    """Convert atomic USDC (6-decimal integer) to an x402 price string.

    Examples:  10000 → "$0.01",  1000000 → "$1.00",  500000 → "$0.50"
    """
    full = f"{atomic / 1_000_000:.6f}"  # e.g. "0.010000"
    integer_part, frac_part = full.split(".")
    stripped = frac_part.rstrip("0")
    # Keep at least 2 decimal places for readability
    if len(stripped) < 2:
        stripped = stripped.ljust(2, "0")
    return f"${integer_part}.{stripped}"


# ─── Requirements cache ───────────────────────────────────────────────────────


def get_payment_requirements() -> list:
    """Return cached PaymentRequirements for /agent/run."""
    if _requirements_cache is None:
        raise RuntimeError("Billing not initialised or disabled")
    return _requirements_cache


def build_402_response_body() -> dict:
    """Build the JSON body for a 402 Payment Required response."""
    reqs = get_payment_requirements()
    return {
        "error": "Payment required",
        "accepts": [r.model_dump() if hasattr(r, "model_dump") else r.__dict__ for r in reqs],
        "x402Version": 2,
    }


def build_402_headers() -> dict[str, str]:
    """Build response headers for a 402 Payment Required response."""
    import base64

    reqs = get_payment_requirements()
    serialised = json.dumps(
        [r.model_dump() if hasattr(r, "model_dump") else r.__dict__ for r in reqs],
        default=str,
    )
    encoded = base64.b64encode(serialised.encode()).decode()
    return {"X-PAYMENT-REQUIRED": encoded}


async def _rebuild_requirements_if_stale() -> None:
    """Rebuild x402 payment requirements when the DB pricing rule has changed.

    Compares the live rule's run_price_usdc against the value used when
    requirements were last built.  No-ops when billing is disabled or when
    the price is unchanged.  Safe to call on every verify_payment() call —
    get_live_pricing() serves from a TTL cache so DB hits are rare.
    """
    global _requirements_cache, _last_requirements_price_usdc

    if _server is None:
        return

    rule = await get_live_pricing()
    if rule is None:
        return

    if rule.run_price_usdc == _last_requirements_price_usdc:
        return  # Price unchanged; nothing to do.

    settings = get_settings()
    from x402.schemas import ResourceConfig

    new_price_str = atomic_usdc_to_price_str(rule.run_price_usdc)
    config = ResourceConfig(
        scheme=settings.x402_scheme,
        network=settings.x402_network,
        pay_to=settings.x402_pay_to_address,
        price=new_price_str,
    )
    _requirements_cache = _server.build_payment_requirements(config)
    _last_requirements_price_usdc = rule.run_price_usdc
    logger.info(
        "Payment requirements updated: run_price_usdc=%d price=%s",
        rule.run_price_usdc,
        new_price_str,
    )


async def verify_payment(payment_header: str) -> BillingResult:
    """Verify a payment header against cached requirements.

    Refreshes payment requirements from the live pricing rule before verifying
    (rate-limited by TTL cache — see pricing_cache_ttl_seconds in config).
    Returns BillingResult with verified=True and stored payload/requirements,
    or verified=False with an error message.
    """
    await _rebuild_requirements_if_stale()

    from x402 import parse_payment_payload

    server = _get_server()
    reqs = get_payment_requirements()

    if not reqs:
        return BillingResult(error="No payment requirements configured")

    try:
        import base64 as _base64

        payload = parse_payment_payload(_base64.b64decode(payment_header))
    except Exception as exc:
        logger.warning("Failed to parse payment header: %s", exc)
        return BillingResult(error=f"Malformed payment header: {exc}")

    # Verify against the first (only) requirement
    requirement = reqs[0]
    try:
        result = await server.verify_payment(payload, requirement)
    except Exception as exc:
        logger.error("Payment verification error: %s", exc, exc_info=True)
        return BillingResult(error=f"Verification failed: {exc}")

    if not result.is_valid:
        reason = result.invalid_reason or result.invalid_message or "invalid signature or amount"
        logger.warning("Payment verification failed: %s (payer=%s)", reason, result.payer)
        return BillingResult(error=f"Payment verification failed: {reason}")

    return BillingResult(
        verified=True,
        payment_payload=payload,
        payment_requirements=requirement,
    )


async def settle_payment(billing_result: BillingResult) -> BillingResult:
    """Settle a verified payment on-chain via the facilitator.

    Mutates and returns the BillingResult with settlement details.
    """
    if not billing_result.verified:
        billing_result.error = "Cannot settle unverified payment"
        return billing_result

    server = _get_server()

    try:
        result = await server.settle_payment(
            billing_result.payment_payload,
            billing_result.payment_requirements,
        )
    except Exception as exc:
        logger.error("Payment settlement error: %s", exc, exc_info=True)
        billing_result.error = f"Settlement failed: {exc}"
        return billing_result

    if not result.success:
        billing_result.error = "Settlement rejected by facilitator"
        return billing_result

    billing_result.settled = True
    billing_result.tx_hash = (
        getattr(result, "tx_hash", "") or getattr(result, "transaction_hash", "") or ""
    )
    # Extract settled amount from the requirement
    req = billing_result.payment_requirements
    billing_result.amount_usdc = int(getattr(req, "amount", "0") or "0")

    return billing_result


# ─── Usage event settlement recording ────────────────────────────────────────


async def record_settlement(
    usage_event_id: str,
    cost_usdc: int,
    settlement_tx: str,
    settlement_status: str = "settled",
) -> None:
    """Update a usage event with settlement details."""
    try:
        pool = _get_pool()
        await pool.execute(
            """
            UPDATE usage_events
            SET cost_usdc = $2, settlement_tx = $3, settlement_status = $4
            WHERE id = $1
            """,
            usage_event_id,
            cost_usdc,
            settlement_tx,
            settlement_status,
        )
    except Exception:
        logger.exception("Failed to record settlement for event=%s", usage_event_id)


# ─── Pricing queries ─────────────────────────────────────────────────────────


async def get_current_pricing() -> PricingRule | None:
    """Return the currently effective pricing rule (direct DB query, no cache)."""
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, name, run_price_usdc, tokens_in_cost_per_1k,
               tokens_out_cost_per_1k, tool_call_cost, effective_from, created_at
        FROM pricing_rules
        WHERE effective_from <= NOW()
        ORDER BY effective_from DESC
        LIMIT 1
        """
    )
    if row is None:
        return None
    return PricingRule(**dict(row))


async def get_live_pricing() -> PricingRule | None:
    """Return the current pricing rule using a TTL cache.

    Uses Redis if available for multi-container consistency, otherwise
    falls back to in-process TTL cache. Re-queries the DB at most once per
    pricing_cache_ttl_seconds. Returns None if no pricing_rules row exists or
    DB is unavailable.
    """
    global _pricing_cache, _pricing_cache_expires, _pricing_lock
    settings = get_settings()
    redis = get_redis()

    # ── Redis path (multi-container) ──────────────────────────────────────
    if redis is not None:
        try:
            key = "teardrop:pricing:active"
            cached_json = await redis.get(key)
            if cached_json is not None:
                data = json.loads(cached_json)
                return PricingRule(**data)
        except Exception as exc:
            logger.warning("Redis pricing cache read failed; falling back to in-process: %s", exc)
            # Fall through to in-process cache

    # ── In-process TTL cache path (single-container fallback) ───────────────
    # Fast path: valid in-process cache.
    if _pricing_cache is not None and time.monotonic() < _pricing_cache_expires:
        return _pricing_cache

    # Pool not yet set (e.g. called before init_billing).
    if _pool is None:
        return None

    # Lazily create the lock (cannot be created at module level on all platforms).
    if _pricing_lock is None:
        _pricing_lock = asyncio.Lock()

    async with _pricing_lock:
        # Double-check after acquiring — another coroutine may have refreshed.
        if _pricing_cache is not None and time.monotonic() < _pricing_cache_expires:
            return _pricing_cache

        try:
            rule = await get_current_pricing()
            if rule is not None:
                _pricing_cache = rule
                _pricing_cache_expires = time.monotonic() + settings.pricing_cache_ttl_seconds

                # Write to Redis if available
                if (redis := get_redis()) is not None:
                    try:
                        key = "teardrop:pricing:active"
                        cached_json = json.dumps(rule.model_dump(), default=str)
                        await redis.setex(key, settings.pricing_cache_ttl_seconds, cached_json)
                    except Exception as exc:
                        logger.warning("Redis pricing cache write failed (non-fatal): %s", exc)

            return rule
        except Exception:
            logger.warning("Failed to refresh pricing cache; serving stale value", exc_info=True)
            return _pricing_cache  # Return stale on DB error rather than crashing.


async def calculate_run_cost_usdc(usage_data: dict) -> int:
    """Calculate the cost of a completed run in atomic USDC (6-decimal integer).

    Uses per-unit token and tool-call rates from the live pricing rule when
    they are non-zero (usage-based pricing).  Falls back to run_price_usdc as
    a flat rate when the active rule has no per-unit rates configured.

    Returns 0 if no pricing rule is available (e.g. DB not yet seeded).

    Formula (usage-based):
        cost = (tokens_in // 1000) * tokens_in_cost_per_1k
             + (tokens_out // 1000) * tokens_out_cost_per_1k
             + tool_calls * tool_call_cost
    """
    rule = await get_live_pricing()
    if rule is None:
        return 0

    tokens_in = int(usage_data.get("tokens_in", 0))
    tokens_out = int(usage_data.get("tokens_out", 0))
    tool_calls = int(usage_data.get("tool_calls", 0))

    has_per_unit_rates = (
        rule.tokens_in_cost_per_1k > 0 or rule.tokens_out_cost_per_1k > 0 or rule.tool_call_cost > 0
    )

    if not has_per_unit_rates:
        # Flat-rate rule: every run costs run_price_usdc.
        return rule.run_price_usdc

    return (
        (tokens_in // 1000) * rule.tokens_in_cost_per_1k
        + (tokens_out // 1000) * rule.tokens_out_cost_per_1k
        + tool_calls * rule.tool_call_cost
    )


async def get_billing_history(
    user_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[dict]:
    """Return recent settled usage events for a user.

    Supports cursor-based pagination: pass the ``created_at`` value of the
    last item returned as ``cursor`` to retrieve the next page.
    """
    pool = _get_pool()
    if cursor is None:
        rows = await pool.fetch(
            """
            SELECT id, run_id, tokens_in, tokens_out, tool_calls, duration_ms,
                   cost_usdc, settlement_tx, settlement_status, created_at
            FROM usage_events
            WHERE user_id = $1 AND settlement_status != 'none'
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, run_id, tokens_in, tokens_out, tool_calls, duration_ms,
                   cost_usdc, settlement_tx, settlement_status, created_at
            FROM usage_events
            WHERE user_id = $1 AND settlement_status != 'none'
              AND created_at < $3
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
            cursor,
        )
    return [dict(r) for r in rows]


async def get_revenue_summary(
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """Aggregate settled revenue within an optional date range."""
    pool = _get_pool()
    query = """
        SELECT COUNT(*) as total_settlements,
               COALESCE(SUM(cost_usdc), 0) as total_revenue_usdc
        FROM usage_events
        WHERE settlement_status = 'settled'
    """
    params: list = []
    idx = 1
    if start is not None:
        query += f" AND created_at >= ${idx}"
        params.append(start)
        idx += 1
    if end is not None:
        query += f" AND created_at <= ${idx}"
        params.append(end)
        idx += 1

    row = await pool.fetchrow(query, *params)
    if row is None:
        return {"total_settlements": 0, "total_revenue_usdc": 0}
    return dict(row)


# ─── Invoice query functions ──────────────────────────────────────────────────


async def get_invoices(
    user_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[dict]:
    """Return per-run invoice records for a user (all runs, not just settled).

    Supports cursor-based pagination: pass the ``created_at`` of the last
    returned item as ``cursor`` to fetch the next page.
    """
    pool = _get_pool()
    if cursor is None:
        rows = await pool.fetch(
            """
            SELECT id, run_id, thread_id, tokens_in, tokens_out, tool_calls,
                   tool_names, duration_ms, cost_usdc, settlement_tx,
                   settlement_status, created_at
            FROM usage_events
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, run_id, thread_id, tokens_in, tokens_out, tool_calls,
                   tool_names, duration_ms, cost_usdc, settlement_tx,
                   settlement_status, created_at
            FROM usage_events
            WHERE user_id = $1 AND created_at < $3
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
            cursor,
        )
    return [dict(r) for r in rows]


async def get_invoice_by_run(run_id: str, user_id: str) -> dict | None:
    """Return a single run receipt, scoped to the authenticated user.

    Returns None if the run_id does not exist or does not belong to user_id.
    """
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, run_id, thread_id, tokens_in, tokens_out, tool_calls,
               tool_names, duration_ms, cost_usdc, settlement_tx,
               settlement_status, created_at
        FROM usage_events
        WHERE run_id = $1 AND user_id = $2
        """,
        run_id,
        user_id,
    )
    return dict(row) if row is not None else None


# ─── Credit system (off-chain prepaid balance for non-SIWE callers) ───────────


async def get_credit_balance(org_id: str) -> int:
    """Return org's current credit balance in atomic USDC (0 if no row yet)."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT balance_usdc FROM org_credits WHERE org_id = $1",
        org_id,
    )
    return int(row["balance_usdc"]) if row is not None else 0


async def verify_credit(org_id: str, min_balance_usdc: int) -> BillingResult:
    """Check that org has sufficient credit to cover at least min_balance_usdc.

    Returns BillingResult(verified=True, billing_method='credit') on success.
    """
    balance = await get_credit_balance(org_id)
    if balance < min_balance_usdc:
        return BillingResult(
            error=(
                f"Insufficient credit: balance {balance} atomic USDC, "
                f"required {min_balance_usdc}. Top up via POST /admin/credits/topup."
            )
        )
    return BillingResult(verified=True, billing_method="credit")


async def debit_credit(org_id: str, amount_usdc: int, reason: str = "") -> bool:
    """Debit amount_usdc from org's credit balance using a serialisable transaction.

    Uses SELECT FOR UPDATE to prevent concurrent double-debits.
    Floors balance at 0 — will not go negative.
    Inserts a row into org_credit_ledger within the same transaction.
    Returns True on success, False on unexpected DB error.
    """
    pool = _get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT balance_usdc FROM org_credits WHERE org_id = $1 FOR UPDATE",
                    org_id,
                )
                if row is None:
                    # Row doesn't exist — nothing to debit
                    return False
                new_balance = max(0, int(row["balance_usdc"]) - amount_usdc)
                await conn.execute(
                    """
                    UPDATE org_credits
                    SET balance_usdc = $2, updated_at = NOW()
                    WHERE org_id = $1
                    """,
                    org_id,
                    new_balance,
                )
                await conn.execute(
                    """
                    INSERT INTO org_credit_ledger
                        (id, org_id, operation, amount_usdc, balance_usdc_after, reason, created_at)
                    VALUES ($1, $2, 'debit', $3, $4, $5, NOW())
                    """,
                    str(uuid.uuid4()),
                    org_id,
                    amount_usdc,
                    new_balance,
                    reason,
                )
        return True
    except Exception:
        logger.exception("debit_credit failed org_id=%s amount=%s", org_id, amount_usdc)
        return False


async def admin_topup_credit(org_id: str, amount_usdc: int, reason: str = "") -> int:
    """Add amount_usdc to org's credit balance (upsert). Returns new balance."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
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
            new_balance = int(row["balance_usdc"])
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
                reason,
            )
    return new_balance


async def get_credit_history(
    org_id: str,
    operation: str | None = None,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[dict]:
    """Return credit ledger entries for an org (cursor paginated, newest first).

    ``operation`` can be ``'debit'``, ``'topup'``, or ``None`` for all.
    ``cursor`` is the ``created_at`` of the last item returned (exclusive).
    """
    pool = _get_pool()
    params: list = [org_id, limit]
    filters = ["org_id = $1"]
    if operation is not None:
        params.append(operation)
        filters.append(f"operation = ${len(params)}")
    if cursor is not None:
        params.append(cursor)
        filters.append(f"created_at < ${len(params)}")
    where = " AND ".join(filters)
    rows = await pool.fetch(
        f"""
        SELECT id, org_id, operation, amount_usdc, balance_usdc_after, reason, created_at
        FROM org_credit_ledger
        WHERE {where}
        ORDER BY created_at DESC
        LIMIT $2
        """,
        *params,
    )
    return [dict(r) for r in rows]


# ─── Stripe (prepaid credit top-up) ──────────────────────────────────────────


async def create_stripe_embedded_session(
    org_id: str, user_id: str, amount_cents: int, return_url: str
) -> dict[str, str]:
    """Create a Stripe Checkout session for embedded checkout (prepaid credit top-up).

    amount_cents is USD cents (100 = $1.00).
    return_url must be an HTTPS URL containing {CHECKOUT_SESSION_ID} for Stripe template substitution.
    Unit conversion: 1 USD cent = 10_000 atomic USDC (1_000_000 = $1.00).

    Returns a dict with 'client_secret' and 'session_id' for the frontend to render the embedded form.
    """
    import stripe  # noqa: PLC0415 — lazy import; only needed when Stripe is configured

    settings = get_settings()
    if not settings.stripe_secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = settings.stripe_secret_key

    amount_usdc = amount_cents * 10_000  # atomic USDC units
    session = await stripe.checkout.Session.create_async(
        mode="payment",
        ui_mode="embedded",
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
        raise RuntimeError(
            f"Stripe returned a session without client_secret (session_id={session.id})"
        )
    return {"client_secret": session.client_secret, "session_id": session.id}


async def handle_stripe_webhook(payload: bytes, sig_header: str) -> None:
    """Verify and process a Stripe webhook event.

    Raises stripe.SignatureVerificationError on invalid signature.
    Raises ValueError on malformed JSON payload.
    Both should result in an HTTP 400 response from the caller.

    Idempotent: duplicate deliveries of the same event are silently ignored
    via the stripe_webhook_events PRIMARY KEY constraint.
    """
    import stripe  # noqa: PLC0415

    settings = get_settings()

    event = stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)

    if event.type != "checkout.session.completed":
        return

    session = event.data.object
    if session.payment_status != "paid":
        return

    org_id: str | None = session.client_reference_id or (
        session.metadata.get("org_id") if session.metadata else None
    )
    if not org_id:
        logger.error("stripe webhook: no org_id in event %s", event.id)
        return

    # Prefer metadata amount (set by us); fall back to Stripe amount_total.
    # Wrap int() conversion — malformed metadata should not raise ValueError,
    # which would be misidentified by the caller as a bad Stripe payload.
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
        return

    # Perform idempotency guard + credit update in a single transaction so that
    # a crash between the two writes can never result in a consumed event with
    # no credit applied (which would be silently skipped on Stripe's retry).
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
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
                # Duplicate delivery — silently ignore
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

    logger.info(
        "stripe webhook: topped up org_id=%s amount_usdc=%s event=%s",
        org_id,
        amount_usdc,
        event.id,
    )


async def get_stripe_session_status(session_id: str, org_id: str) -> dict[str, Any]:
    """Retrieve a Stripe Checkout session's status and optionally the updated credit balance.

    Validates that the session belongs to the requested org_id (via client_reference_id).
    If session is complete, includes 'new_balance_fmt' (formatted credit balance in $X.XX).

    Raises PermissionError if session's org_id does not match the requested org_id.
    Raises stripe.error.InvalidRequestError if session_id does not exist.
    """
    import stripe  # noqa: PLC0415

    settings = get_settings()
    if not settings.stripe_secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = settings.stripe_secret_key

    session = await stripe.checkout.Session.retrieve_async(session_id)

    # Verify ownership: session.client_reference_id must match the requested org_id
    if session.client_reference_id != org_id:
        raise PermissionError(f"Session {session_id} does not belong to org_id {org_id}")

    result: dict[str, Any] = {"status": session.status}

    # If payment is complete, fetch and include the new credit balance
    if session.status == "complete":
        balance_usdc = await get_credit_balance(org_id)
        result["new_balance_fmt"] = atomic_usdc_to_price_str(balance_usdc)

    return result


# ─── USDC on-chain credit top-up ─────────────────────────────────────────────


def build_usdc_topup_requirements(amount_usdc: int) -> list:
    """Build x402 PaymentRequirements for a USDC on-chain top-up of amount_usdc.

    amount_usdc is in atomic USDC units (6 decimals) — e.g. 1_000_000 = $1.00.
    Raises RuntimeError if billing is not initialised (BILLING_ENABLED=false).
    The caller (endpoint) should translate RuntimeError to HTTP 503.
    """
    from x402.schemas import ResourceConfig  # noqa: PLC0415

    settings = get_settings()
    server = _get_server()  # Raises RuntimeError if not initialised.
    config = ResourceConfig(
        scheme=settings.x402_scheme,
        network=settings.x402_network,
        pay_to=settings.x402_pay_to_address,
        price=atomic_usdc_to_price_str(amount_usdc),
    )
    return server.build_payment_requirements(config)


async def verify_and_settle_usdc_topup(
    payment_header: str,
    amount_usdc: int,
) -> BillingResult:
    """Verify and immediately settle a USDC top-up payment header on-chain.

    payment_header is a base64-encoded EIP-3009 PaymentPayload (same format
    as the X-PAYMENT header used on /agent/run).

    Verification checks: EIP-3009 signature valid, amount matches amount_usdc,
    pay_to matches the treasury address.  Settlement submits the authorised
    transfer to the x402 facilitator and returns a tx_hash.

    Returns BillingResult with settled=True and tx_hash on success, or
    BillingResult with error set on any failure.
    """
    import base64 as _base64  # noqa: PLC0415

    from x402 import parse_payment_payload  # noqa: PLC0415

    server = _get_server()
    requirements = build_usdc_topup_requirements(amount_usdc)

    if not requirements:
        return BillingResult(error="No payment requirements could be built")

    try:
        payload = parse_payment_payload(_base64.b64decode(payment_header))
    except Exception as exc:
        logger.warning("usdc_topup: failed to parse payment header: %s", exc)
        return BillingResult(error=f"Malformed payment header: {exc}")

    requirement = requirements[0]

    try:
        verify_result = await server.verify_payment(payload, requirement)
    except Exception as exc:
        logger.error("usdc_topup: verification error: %s", exc, exc_info=True)
        return BillingResult(error=f"Verification failed: {exc}")

    if not verify_result.is_valid:
        reason = (
            verify_result.invalid_reason
            or verify_result.invalid_message
            or "invalid signature or amount"
        )
        logger.warning("usdc_topup: verification failed: %s", reason)
        return BillingResult(error=f"Payment verification failed: {reason}")

    try:
        settle_result = await server.settle_payment(payload, requirement)
    except Exception as exc:
        logger.error("usdc_topup: settlement error: %s", exc, exc_info=True)
        return BillingResult(error=f"Settlement failed: {exc}")

    if not settle_result.success:
        logger.error("usdc_topup: facilitator rejected settlement")
        return BillingResult(error="Settlement rejected by facilitator")

    tx_hash = (
        getattr(settle_result, "tx_hash", "")
        or getattr(settle_result, "transaction_hash", "")
        or ""
    )
    logger.info("usdc_topup: settled tx_hash=%s amount_usdc=%s", tx_hash, amount_usdc)
    return BillingResult(verified=True, settled=True, tx_hash=tx_hash, amount_usdc=amount_usdc)


async def credit_usdc_topup(org_id: str, amount_usdc: int, tx_hash: str) -> int | None:
    """Credit amount_usdc to org's balance after a confirmed on-chain top-up.

    Idempotent: if tx_hash was already processed, returns None (duplicate).
    On success, inserts into usdc_topup_events (idempotency guard), upserts
    org_credits, and appends an org_credit_ledger row — all in one transaction.

    Returns new balance_usdc on success, None on duplicate tx_hash.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            guard_row = await conn.fetchrow(
                """
                INSERT INTO usdc_topup_events (tx_hash, org_id, amount_usdc)
                VALUES ($1, $2, $3)
                ON CONFLICT (tx_hash) DO NOTHING
                RETURNING tx_hash
                """,
                tx_hash,
                org_id,
                amount_usdc,
            )
            if guard_row is None:
                logger.info("usdc_topup: duplicate tx_hash=%s ignored", tx_hash)
                return None

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
                f"usdc_onchain:{tx_hash}",
            )

    logger.info(
        "usdc_topup: credited org_id=%s amount_usdc=%s new_balance=%s tx_hash=%s",
        org_id,
        amount_usdc,
        new_balance,
        tx_hash,
    )
    return new_balance
