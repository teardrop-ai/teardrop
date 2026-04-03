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
from datetime import datetime, timezone

import asyncpg
from pydantic import BaseModel, Field

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

    facilitator = HTTPFacilitatorClient(
        FacilitatorConfig(url=settings.x402_facilitator_url)
    )
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
        logger.warning(
            "No pricing_rules row found; using config fallback price=%s", price_str
        )

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
        "accepts": [
            r.model_dump() if hasattr(r, "model_dump") else r.__dict__
            for r in reqs
        ],
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
    billing_result.tx_hash = getattr(result, "tx_hash", "") or getattr(result, "transaction_hash", "") or ""
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

    Re-queries the DB at most once per pricing_cache_ttl_seconds.  Safe to
    call on every request — the fast path is a single monotonic clock compare.
    Returns None if no pricing_rules row exists or DB is unavailable.
    """
    global _pricing_cache, _pricing_cache_expires, _pricing_lock

    # Fast path: valid cache.
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
            _pricing_cache = rule
            settings = get_settings()
            _pricing_cache_expires = time.monotonic() + settings.pricing_cache_ttl_seconds
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
        rule.tokens_in_cost_per_1k > 0
        or rule.tokens_out_cost_per_1k > 0
        or rule.tool_call_cost > 0
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


async def debit_credit(org_id: str, amount_usdc: int) -> bool:
    """Debit amount_usdc from org's credit balance using a serialisable transaction.

    Uses SELECT FOR UPDATE to prevent concurrent double-debits.
    Floors balance at 0 — will not go negative.
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
        return True
    except Exception:
        logger.exception("debit_credit failed org_id=%s amount=%s", org_id, amount_usdc)
        return False


async def admin_topup_credit(org_id: str, amount_usdc: int) -> int:
    """Add amount_usdc to org's credit balance (upsert). Returns new balance."""
    pool = _get_pool()
    row = await pool.fetchrow(
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
    return int(row["balance_usdc"])
