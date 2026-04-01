"""x402 billing layer for Teardrop.

Provides server-side x402 payment verification and settlement for
the /agent/run SSE endpoint using the exact scheme (flat per-run pricing).

Manual wiring (not middleware) because SSE streaming is incompatible with
the standard request/response middleware pattern — we must verify before
streaming begins and settle after the stream completes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import asyncpg
from pydantic import BaseModel, Field

from config import get_settings

logger = logging.getLogger(__name__)

# ─── Lazy x402 imports (only when billing is enabled) ─────────────────────────

_server = None  # x402ResourceServer instance
_requirements_cache: list | None = None  # cached PaymentRequirements for /agent/run


def _get_server():
    """Return the initialized x402ResourceServer, or raise if not ready."""
    if _server is None:
        raise RuntimeError("Billing not initialised — call init_billing() first")
    return _server


# ─── Initialisation ──────────────────────────────────────────────────────────


async def init_billing(pool: asyncpg.Pool) -> None:
    """Initialise x402 resource server and cache payment requirements.

    Call during app lifespan startup when billing_enabled=True.
    """
    global _server, _requirements_cache, _pool

    settings = get_settings()
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

    # Build and cache payment requirements for /agent/run
    config = ResourceConfig(
        scheme="exact",
        network=settings.x402_network,
        pay_to=settings.x402_pay_to_address,
        price=settings.x402_run_price,
    )
    _requirements_cache = server.build_payment_requirements(config)
    _server = server
    _pool = pool

    logger.info(
        "Billing initialised: network=%s pay_to=%s price=%s",
        settings.x402_network,
        settings.x402_pay_to_address,
        settings.x402_run_price,
    )


async def close_billing() -> None:
    """Release billing resources."""
    global _server, _requirements_cache, _pool
    _server = None
    _requirements_cache = None
    _pool = None
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


# ─── Payment flow helpers ────────────────────────────────────────────────────


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


async def verify_payment(payment_header: str) -> BillingResult:
    """Verify a payment header against cached requirements.

    Returns BillingResult with verified=True and stored payload/requirements,
    or verified=False with an error message.
    """
    from x402 import parse_payment_payload

    server = _get_server()
    reqs = get_payment_requirements()

    if not reqs:
        return BillingResult(error="No payment requirements configured")

    try:
        payload = parse_payment_payload(payment_header)
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
        return BillingResult(error="Payment verification failed: invalid signature or amount")

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
    """Return the currently effective pricing rule."""
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


async def get_billing_history(
    user_id: str,
    limit: int = 50,
) -> list[dict]:
    """Return recent settled usage events for a user."""
    pool = _get_pool()
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
