# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
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
from typing import Any, Awaitable, Callable, Generic, TypeVar

import asyncpg
from pydantic import BaseModel, Field

from cache import get_redis
from config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Single-value TTL cache: Redis-first with in-process fallback.

    Preserves the existing graceful-degradation contract:
    - Redis is consulted first; on read failure we fall through to in-process.
    - In-process tier holds the last successfully-loaded value with a TTL.
    - Loader failures serve the stale in-process value if present, else the
      configured ``stale_default``.

    Designed for single-tenant caches (one logical value per cache instance).
    Keyed caches (e.g. per-(provider, model) pricing) use a different shape
    and remain inline.
    """

    def __init__(
        self,
        *,
        name: str,
        redis_key: str,
        ttl_seconds_fn: Callable[[], int],
        loader: Callable[[], Awaitable[T | None]],
        serialize: Callable[[T], str],
        deserialize: Callable[[str], T | None],
        cache_when: Callable[[T | None], bool] = lambda v: v is not None,
        stale_default: T | None = None,
    ) -> None:
        self._name = name
        self._redis_key = redis_key
        self._ttl_fn = ttl_seconds_fn
        self._loader = loader
        self._serialize = serialize
        self._deserialize = deserialize
        self._cache_when = cache_when
        self._stale_default = stale_default
        self._value: T | None = None
        self._expires: float = 0.0
        self._lock: asyncio.Lock | None = None

    async def get(self) -> T | None:
        # ── Redis path (multi-container) ──────────────────────────────────
        redis = get_redis()
        if redis is not None:
            try:
                raw = await redis.get(self._redis_key)
                if raw is not None:
                    return self._deserialize(raw)
            except Exception as exc:
                logger.warning(
                    "Redis %s cache read failed; falling back to in-process: %s",
                    self._name,
                    exc,
                )

        # ── In-process fast path ──────────────────────────────────────────
        if self._value is not None and time.monotonic() < self._expires:
            return self._value

        if self._lock is None:
            self._lock = asyncio.Lock()

        async with self._lock:
            # Double-check after acquiring; another coroutine may have refreshed.
            if self._value is not None and time.monotonic() < self._expires:
                return self._value

            try:
                value = await self._loader()
            except Exception:
                logger.warning(
                    "Failed to refresh %s cache; serving stale value",
                    self._name,
                    exc_info=True,
                )
                if self._value is not None:
                    return self._value
                return self._stale_default

            ttl = self._ttl_fn()
            if self._cache_when(value):
                self._value = value
                self._expires = time.monotonic() + ttl

                redis = get_redis()
                if redis is not None and value is not None:
                    try:
                        await redis.setex(self._redis_key, ttl, self._serialize(value))
                    except Exception as exc:
                        logger.warning(
                            "Redis %s cache write failed (non-fatal): %s",
                            self._name,
                            exc,
                        )

            return value

    async def invalidate(self) -> None:
        """Drop the in-process value and delete the Redis key."""
        self._value = None
        self._expires = 0.0
        redis = get_redis()
        if redis is not None:
            try:
                await redis.delete(self._redis_key)
            except Exception as exc:
                logger.warning(
                    "Redis %s cache invalidation failed (non-fatal): %s",
                    self._name,
                    exc,
                )

    def reset(self) -> None:
        """Synchronous reset of in-process tier only (for shutdown paths)."""
        self._value = None
        self._expires = 0.0

# ─── Lazy x402 imports (only when billing is enabled) ─────────────────────────

_server = None  # x402ResourceServer instance
_requirements_cache: list | None = None  # cached PaymentRequirements for /agent/run
_exact_requirements_cache: list | None = None  # exact-scheme requirements (always built)
_upto_requirements_cache: list | None = None   # upto-scheme requirements (when opted in)

# ─── Pricing TTL cache ────────────────────────────────────────────────────────
# Note: ``_live_pricing_cache`` and ``_tool_overrides_cache_obj`` (declared
# later, after ``get_current_pricing`` and ``get_current_tool_overrides``
# are defined) hold the singleton-style pricing caches. The keyed per-model
# cache below remains inline because of its different shape.

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
    global _exact_requirements_cache, _upto_requirements_cache

    settings = get_settings()

    # Always store pool — pricing queries run regardless of billing_enabled.
    _pool = pool

    if not settings.billing_enabled:
        logger.info("Billing disabled — skipping x402 initialisation")
        return

    if not settings.x402_pay_to_address:
        raise RuntimeError("billing_enabled=True but x402_pay_to_address is empty")

    from x402 import ResourceConfig, x402ResourceServer
    from x402.http import HTTPFacilitatorClient
    from x402.http.facilitator_client_base import FacilitatorConfig
    from x402.mechanisms.evm.exact import ExactEvmServerScheme

    facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=settings.x402_facilitator_url))
    server = x402ResourceServer(facilitator)
    server.register(settings.x402_network, ExactEvmServerScheme())

    # Register upto scheme alongside exact when the operator has opted in.
    if settings.x402_scheme == "upto":
        try:
            from x402.mechanisms.evm.upto import (  # type: ignore[import]
                UptoEvmServerScheme,
            )
        except ImportError as exc:
            raise RuntimeError(
                "x402 upto scheme is not available in the installed package. "
                "Upgrade: pip install 'x402[fastapi,evm]>=2.8.0'"
            ) from exc

        server.register(settings.x402_network, UptoEvmServerScheme())

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

    # Always build exact requirements (backward-compat fallback for all clients).
    exact_config = ResourceConfig(
        scheme="exact",
        network=settings.x402_network,
        pay_to=settings.x402_pay_to_address,
        price=price_str,
    )
    _exact_requirements_cache = server.build_payment_requirements(exact_config)

    if settings.x402_scheme == "upto":
        # Build upto requirements with the max_amount ceiling.
        upto_config = ResourceConfig(
            scheme="upto",
            network=settings.x402_network,
            pay_to=settings.x402_pay_to_address,
            price=settings.x402_upto_max_amount,
        )
        _upto_requirements_cache = server.build_payment_requirements(upto_config)
        # Advertise upto first (preferred), exact second (fallback for clients
        # that haven't approved the Permit2 contract yet).
        _requirements_cache = [*_upto_requirements_cache, *_exact_requirements_cache]
    else:
        _upto_requirements_cache = None
        _requirements_cache = list(_exact_requirements_cache)

    advertised_price = (
        settings.x402_upto_max_amount if settings.x402_scheme == "upto" else price_str
    )
    logger.info(
        "Billing initialised: network=%s pay_to=%s price=%s scheme=%s",
        settings.x402_network,
        settings.x402_pay_to_address,
        advertised_price,
        settings.x402_scheme,
    )


async def close_billing() -> None:
    """Release billing resources."""
    global _server, _requirements_cache, _pool, _last_requirements_price_usdc
    global _exact_requirements_cache, _upto_requirements_cache
    _server = None
    _requirements_cache = None
    _exact_requirements_cache = None
    _upto_requirements_cache = None
    _pool = None
    _last_requirements_price_usdc = -1
    _live_pricing_cache.reset()
    _tool_overrides_cache_obj.reset()
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


class ToolPricingOverride(BaseModel):
    tool_name: str
    cost_usdc: int  # atomic USDC, e.g. 15000 = $0.015
    description: str = ""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
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
    # Distinguishes exact vs upto within x402. Controls whether settle_payment()
    # passes actual_cost_usdc to the facilitator.
    scheme: str = "exact"


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
    global _exact_requirements_cache, _upto_requirements_cache

    if _server is None:
        return

    rule = await get_live_pricing()
    if rule is None:
        return

    if rule.run_price_usdc == _last_requirements_price_usdc:
        return  # Price unchanged; nothing to do.

    settings = get_settings()
    from x402 import ResourceConfig

    new_price_str = atomic_usdc_to_price_str(rule.run_price_usdc)

    # Always rebuild exact requirements.
    exact_config = ResourceConfig(
        scheme="exact",
        network=settings.x402_network,
        pay_to=settings.x402_pay_to_address,
        price=new_price_str,
    )
    _exact_requirements_cache = _server.build_payment_requirements(exact_config)

    if settings.x402_scheme == "upto":
        upto_config = ResourceConfig(
            scheme="upto",
            network=settings.x402_network,
            pay_to=settings.x402_pay_to_address,
            price=settings.x402_upto_max_amount,
        )
        _upto_requirements_cache = _server.build_payment_requirements(upto_config)
        _requirements_cache = [*_upto_requirements_cache, *_exact_requirements_cache]
    else:
        _upto_requirements_cache = None
        _requirements_cache = list(_exact_requirements_cache)

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

    # Try each requirement (e.g. [upto, exact]) — the first valid match wins.
    # This allows clients with Permit2 approval to use upto while clients
    # without it fall back to exact.
    last_error = "No payment requirements matched"
    for requirement in reqs:
        try:
            result = await server.verify_payment(payload, requirement)
        except Exception as exc:
            logger.debug("Verification attempt failed for scheme=%s: %s",
                         getattr(requirement, "scheme", "?"), exc)
            last_error = f"Verification failed: {exc}"
            continue

        if result.is_valid:
            detected_scheme = getattr(requirement, "scheme", "exact") or "exact"
            return BillingResult(
                verified=True,
                payment_payload=payload,
                payment_requirements=requirement,
                scheme=detected_scheme,
            )

        reason = result.invalid_reason or result.invalid_message or "invalid signature or amount"
        logger.debug("Payment verification failed for scheme=%s: %s (payer=%s)",
                     getattr(requirement, "scheme", "?"), reason, result.payer)
        last_error = f"Payment verification failed: {reason}"

    logger.warning("All payment requirements failed verification: %s", last_error)
    return BillingResult(error=last_error)


async def settle_payment(
    billing_result: BillingResult,
    actual_cost_usdc: int | None = None,
) -> BillingResult:
    """Settle a verified payment on-chain via the facilitator.

    For upto scheme, *actual_cost_usdc* is the computed usage cost that the
    facilitator will settle (must be ≤ the max_amount the client signed).
    For exact scheme, this parameter is ignored.

    Mutates and returns the BillingResult with settlement details.
    """
    if not billing_result.verified:
        billing_result.error = "Cannot settle unverified payment"
        return billing_result

    server = _get_server()

    try:
        if billing_result.scheme == "upto" and actual_cost_usdc is not None:
            # Guard: floor at zero to prevent contract revert on negative amount.
            actual_cost_usdc = max(0, actual_cost_usdc)
            # upto: settle the actual usage cost, not the max the client signed.
            # x402ResourceServer.settle_payment() has no actual_amount param — the
            # correct pattern is to clone requirements with amount overridden to the
            # actual cost in atomic units before passing to the facilitator.
            req = billing_result.payment_requirements
            settled_req = req.model_copy(update={"amount": str(actual_cost_usdc)})
            result = await server.settle_payment(
                billing_result.payment_payload,
                settled_req,
            )
        else:
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

    # For upto, record the actual cost we settled; for exact, use the requirement amount.
    if billing_result.scheme == "upto" and actual_cost_usdc is not None:
        billing_result.amount_usdc = actual_cost_usdc
    else:
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


async def get_current_pricing_for_model(
    provider: str, model: str, *, is_byok: bool = False
) -> PricingRule | None:
    """Return the most specific pricing rule for a provider/model (direct DB query).

    Resolution order: exact model match → provider-level → global default.
    When *is_byok* is True, only BYOK-tier rows (``is_byok = TRUE``) are
    considered, falling back through the same hierarchy.
    """
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, name, run_price_usdc, tokens_in_cost_per_1k,
               tokens_out_cost_per_1k, tool_call_cost, effective_from, created_at
        FROM pricing_rules
        WHERE effective_from <= NOW()
          AND is_byok = $3
          AND (
            (provider = $1 AND model = $2)
            OR (provider = $1 AND model = '')
            OR (provider = '' AND model = '')
          )
        ORDER BY
          CASE
            WHEN provider = $1 AND model = $2 THEN 0
            WHEN provider = $1 AND model = '' THEN 1
            ELSE 2
          END,
          effective_from DESC
        LIMIT 1
        """,
        provider,
        model,
        is_byok,
    )
    if row is None:
        return None
    return PricingRule(**dict(row))


# ─── Per-model pricing TTL cache ─────────────────────────────────────────────

# Cache key format: "{provider}:{model}:{is_byok}" to keep BYOK and standard
# pricing separate in the same dict.
_model_pricing_cache: dict[str, tuple[PricingRule | None, float]] = {}
_model_pricing_lock: asyncio.Lock | None = None


async def get_live_pricing_for_model(
    provider: str, model: str, *, is_byok: bool = False
) -> PricingRule | None:
    """Return model-specific pricing using a TTL cache.

    Falls back through: model-specific → provider-level → global default.
    When *is_byok* is True, resolves from the BYOK orchestration rate table
    (seeded by migration 041) rather than the standard cost table.
    """
    global _model_pricing_lock

    if not provider and not model:
        return await get_live_pricing()

    settings = get_settings()
    cache_key = f"{provider}:{model}:{is_byok}"
    redis = get_redis()

    # Redis path
    if redis is not None:
        try:
            rkey = f"teardrop:pricing:{provider}:{model}:{'byok' if is_byok else 'std'}"
            cached_json = await redis.get(rkey)
            if cached_json is not None:
                data = json.loads(cached_json)
                if data is None:
                    return None
                return PricingRule(**data)
        except Exception as exc:
            logger.warning("Redis model pricing cache read failed: %s", exc)

    # In-process fast path
    entry = _model_pricing_cache.get(cache_key)
    if entry is not None and time.monotonic() < entry[1]:
        return entry[0]

    if _pool is None:
        return None

    if _model_pricing_lock is None:
        _model_pricing_lock = asyncio.Lock()

    async with _model_pricing_lock:
        entry = _model_pricing_cache.get(cache_key)
        if entry is not None and time.monotonic() < entry[1]:
            return entry[0]

        try:
            rule = await get_current_pricing_for_model(provider, model, is_byok=is_byok)
            expires = time.monotonic() + settings.pricing_cache_ttl_seconds
            _model_pricing_cache[cache_key] = (rule, expires)

            if (redis := get_redis()) is not None:
                try:
                    rkey = f"teardrop:pricing:{provider}:{model}:{'byok' if is_byok else 'std'}"
                    payload = json.dumps(rule.model_dump(mode="json") if rule else None, default=str)
                    await redis.setex(rkey, settings.pricing_cache_ttl_seconds, payload)
                except Exception as exc:
                    logger.warning("Redis model pricing cache write failed: %s", exc)

            return rule
        except Exception:
            logger.warning("Failed to refresh model pricing cache", exc_info=True)
            if entry is not None:
                return entry[0]
            return None


async def _load_live_pricing() -> "PricingRule | None":
    """Loader for the live-pricing TTL cache. Returns None when DB is unset."""
    if _pool is None:
        return None
    return await get_current_pricing()


async def _load_tool_overrides() -> dict[str, int]:
    """Loader for the tool-pricing-overrides TTL cache. Returns {} when DB is unset."""
    if _pool is None:
        return {}
    return await get_current_tool_overrides()


_live_pricing_cache: TTLCache[PricingRule] = TTLCache(
    name="pricing",
    redis_key="teardrop:pricing:active",
    ttl_seconds_fn=lambda: get_settings().pricing_cache_ttl_seconds,
    loader=_load_live_pricing,
    serialize=lambda v: json.dumps(v.model_dump(), default=str),
    deserialize=lambda raw: PricingRule(**json.loads(raw)),
    cache_when=lambda v: v is not None,
    stale_default=None,
)

_tool_overrides_cache_obj: TTLCache[dict[str, int]] = TTLCache(
    name="tool overrides",
    redis_key="teardrop:pricing:tool_overrides",
    ttl_seconds_fn=lambda: get_settings().pricing_cache_ttl_seconds,
    loader=_load_tool_overrides,
    serialize=lambda v: json.dumps(v),
    deserialize=lambda raw: json.loads(raw),
    cache_when=lambda v: v is not None,
    stale_default={},
)


async def get_live_pricing() -> PricingRule | None:
    """Return the current pricing rule using a TTL cache.

    Uses Redis if available for multi-container consistency, otherwise
    falls back to in-process TTL cache. Re-queries the DB at most once per
    pricing_cache_ttl_seconds. Returns None if no pricing_rules row exists or
    DB is unavailable.
    """
    return await _live_pricing_cache.get()



# ─── Tool pricing override queries ───────────────────────────────────────────


async def get_current_tool_overrides() -> dict[str, int]:
    """Return all tool pricing overrides as a {tool_name: cost_usdc} dict (direct DB query)."""
    pool = _get_pool()
    rows = await pool.fetch("SELECT tool_name, cost_usdc FROM tool_pricing_overrides")
    return {row["tool_name"]: row["cost_usdc"] for row in rows}


async def get_tool_pricing_overrides() -> dict[str, int]:
    """Return tool pricing overrides using a TTL cache.

    Uses Redis if available for multi-container consistency, otherwise falls
    back to an in-process TTL cache.  Returns an empty dict (never None) when
    no overrides exist — callers should treat it as "use global default."
    """
    result = await _tool_overrides_cache_obj.get()
    return result if result is not None else {}


async def upsert_tool_pricing_override(
    tool_name: str, cost_usdc: int, description: str
) -> None:
    """Insert or update a tool pricing override and invalidate the cache."""
    pool = _get_pool()
    await pool.execute(
        """
        INSERT INTO tool_pricing_overrides (tool_name, cost_usdc, description)
        VALUES ($1, $2, $3)
        ON CONFLICT (tool_name) DO UPDATE
            SET cost_usdc = EXCLUDED.cost_usdc,
                description = EXCLUDED.description,
                updated_at = NOW()
        """,
        tool_name,
        cost_usdc,
        description,
    )
    await _tool_overrides_cache_obj.invalidate()


async def delete_tool_pricing_override(tool_name: str) -> bool:
    """Delete a tool pricing override. Returns True if a row was deleted."""
    pool = _get_pool()
    result = await pool.execute(
        "DELETE FROM tool_pricing_overrides WHERE tool_name = $1", tool_name
    )
    deleted = result.split()[-1] != "0"
    await _tool_overrides_cache_obj.invalidate()
    return deleted


async def calculate_run_cost_usdc(
    usage_data: dict, provider: str = "", model: str = ""
) -> int:
    """Calculate the cost of a completed run in atomic USDC (6-decimal integer).

    Uses per-unit token and tool-call rates from the live pricing rule when
    they are non-zero (usage-based pricing).  Falls back to run_price_usdc as
    a flat rate when the active rule has no per-unit rates configured.

    When *provider* and *model* are given, attempts to use a model-specific
    pricing rule first, falling back to the global default.

    When tool_names is provided in usage_data, each tool is billed at its
    individual override cost (from tool_pricing_overrides table) with the
    global tool_call_cost as the fallback.  If tool_names is absent or shorter
    than tool_calls, the remaining calls are billed at the global default.

    Returns 0 if no pricing rule is available (e.g. DB not yet seeded).

    Formula (usage-based):
        cost = (tokens_in // 1000) * tokens_in_cost_per_1k
             + (tokens_out // 1000) * tokens_out_cost_per_1k
             + sum(override.get(name, tool_call_cost) for name in tool_names)
             + remaining_unnamed_calls * tool_call_cost
    """

    if provider and model:
        rule = await get_live_pricing_for_model(provider, model)
    else:
        rule = await get_live_pricing()
    if rule is None:
        return 0

    tokens_in = int(usage_data.get("tokens_in", 0))
    tokens_out = int(usage_data.get("tokens_out", 0))
    tool_calls = int(usage_data.get("tool_calls", 0))
    tool_names: list[str] = usage_data.get("tool_names") or []

    has_per_unit_rates = (
        rule.tokens_in_cost_per_1k > 0 or rule.tokens_out_cost_per_1k > 0 or rule.tool_call_cost > 0
    )

    if not has_per_unit_rates:
        # Flat-rate rule: every run costs run_price_usdc.
        return rule.run_price_usdc

    token_cost = (
        (tokens_in // 1000) * rule.tokens_in_cost_per_1k
        + (tokens_out // 1000) * rule.tokens_out_cost_per_1k
    )

    if tool_names:
        overrides = await get_tool_pricing_overrides()
        named_cost = sum(
            overrides.get(name, rule.tool_call_cost) for name in tool_names
        )
        # Defensive fallback: bill any gap (e.g. tool_calls counted but name not recorded)
        unnamed_calls = max(0, tool_calls - len(tool_names))
        tool_cost = named_cost + unnamed_calls * rule.tool_call_cost
    else:
        tool_cost = tool_calls * rule.tool_call_cost

    return token_cost + tool_cost


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
                   cost_usdc, platform_fee_usdc, settlement_tx, settlement_status, created_at
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
                   cost_usdc, platform_fee_usdc, settlement_tx, settlement_status, created_at
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
                   tool_names, duration_ms, cost_usdc, platform_fee_usdc, settlement_tx,
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
                   tool_names, duration_ms, cost_usdc, platform_fee_usdc, settlement_tx,
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
               tool_names, duration_ms, cost_usdc, platform_fee_usdc, settlement_tx,
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
    """Check that org has sufficient credit and is within spending limits.

    Returns BillingResult(verified=True, billing_method='credit') on success.
    Checks: (1) not paused, (2) sufficient balance, (3) daily spending limit.
    """
    pool = _get_pool()

    # Fetch credit row with spending config in one query.
    row = await pool.fetchrow(
        "SELECT balance_usdc, spending_limit_usdc, is_paused FROM org_credits WHERE org_id = $1",
        org_id,
    )
    balance = int(row["balance_usdc"]) if row else 0
    spending_limit = int(row["spending_limit_usdc"]) if row else 0
    is_paused = bool(row["is_paused"]) if row else False

    # Check 1: admin pause.
    if is_paused:
        return BillingResult(
            error="Org billing is paused by admin. Contact your administrator."
        )

    # Check 2: sufficient balance.
    if balance < min_balance_usdc:
        return BillingResult(
            error=(
                f"Insufficient credit: balance {balance} atomic USDC, "
                f"required {min_balance_usdc}. Top up via POST /admin/credits/topup."
            )
        )

    # Check 3: daily spending limit (24h rolling window).
    if spending_limit > 0:
        daily_row = await pool.fetchrow(
            """
            SELECT COALESCE(SUM(amount_usdc), 0) AS daily_spend
            FROM org_credit_ledger
            WHERE org_id = $1
              AND operation = 'debit'
              AND created_at >= NOW() - INTERVAL '24 hours'
            """,
            org_id,
        )
        daily_spend = int(daily_row["daily_spend"]) if daily_row else 0
        if daily_spend + min_balance_usdc > spending_limit:
            return BillingResult(
                error=(
                    f"Daily spending limit reached: {daily_spend} of "
                    f"{spending_limit} atomic USDC used in the last 24 hours."
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
    return_url must be an HTTPS URL containing {CHECKOUT_SESSION_ID} for Stripe
    template substitution.
    Unit conversion: 1 USD cent = 10_000 atomic USDC (1_000_000 = $1.00).

    Returns a dict with 'client_secret' and 'session_id' for the frontend to
    render the embedded form.
    """
    import stripe  # noqa: PLC0415 — lazy import; only needed when Stripe is configured

    settings = get_settings()
    if not settings.stripe_secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = settings.stripe_secret_key

    amount_usdc = amount_cents * 10_000  # atomic USDC units
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

    if not sig_header:
        raise ValueError("Missing Stripe-Signature header")

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
    try:
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
    except Exception:
        logger.exception(
            "stripe webhook: DB error processing event=%s org_id=%s amount_usdc=%s",
            event.id,
            org_id,
            amount_usdc,
        )
        raise  # Re-raise → FastAPI returns 500 → Stripe retries

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
    from x402 import ResourceConfig  # noqa: PLC0415

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


# ─── Settlement retry queue ───────────────────────────────────────────────────


async def enqueue_failed_settlement(
    usage_event_id: str,
    org_id: str,
    run_id: str,
    billing_method: str,
    amount_usdc: int,
    payment_payload: str | None = None,
) -> None:
    """Insert a failed settlement into the retry queue.

    Fire-and-forget safe — logs errors but never raises.
    """
    try:
        pool = _get_pool()
        settings = get_settings()
        await pool.execute(
            """
            INSERT INTO pending_settlements
                (id, usage_event_id, org_id, run_id, billing_method,
                 amount_usdc, payment_payload, max_retries, next_retry_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW() + INTERVAL '2 seconds')
            """,
            str(uuid.uuid4()),
            usage_event_id,
            org_id,
            run_id,
            billing_method,
            amount_usdc,
            payment_payload,
            settings.settlement_max_retries,
        )
        logger.info(
            "Enqueued failed settlement for retry: run_id=%s method=%s",
            run_id,
            billing_method,
        )
    except Exception:
        logger.exception(
            "Failed to enqueue settlement for retry: run_id=%s", run_id
        )


async def process_pending_settlements() -> int:
    """Process pending settlements that are due for retry.

    Returns the number of settlements successfully processed.
    Called by the background retry worker in app.py.
    """
    pool = _get_pool()
    processed = 0

    try:
        rows = await pool.fetch(
            """
            SELECT id, usage_event_id, org_id, run_id, billing_method,
                   amount_usdc, payment_payload, retry_count, max_retries
            FROM pending_settlements
            WHERE status IN ('pending', 'retrying')
              AND next_retry_at <= NOW()
            ORDER BY next_retry_at
            LIMIT 20
            FOR UPDATE SKIP LOCKED
            """,
        )
    except Exception:
        logger.exception("Failed to query pending settlements")
        return 0

    for row in rows:
        settlement_id = row["id"]
        billing_method = row["billing_method"]
        retry_count = row["retry_count"] + 1
        max_retries = row["max_retries"]
        success = False
        error_msg = ""

        try:
            if billing_method == "credit":
                success = await debit_credit(
                    row["org_id"], row["amount_usdc"], reason=f"run:{row['run_id']}"
                )
                if not success:
                    error_msg = "debit_credit returned False"
            else:
                # x402: re-attempt settlement is not possible after the fact
                # (the payment payload is single-use). Mark as exhausted.
                error_msg = "x402 settlements cannot be retried after initial failure"
                retry_count = max_retries  # force exhaustion
        except Exception as exc:
            error_msg = str(exc)
            logger.warning(
                "Settlement retry failed: id=%s error=%s", settlement_id, exc
            )

        if success:
            await pool.execute(
                """
                UPDATE pending_settlements
                SET status = 'settled', retry_count = $2, last_error = ''
                WHERE id = $1
                """,
                settlement_id,
                retry_count,
            )
            await record_settlement(
                row["usage_event_id"], row["amount_usdc"], "", "settled"
            )
            processed += 1
            logger.info(
                "Settlement retry succeeded: id=%s run_id=%s attempt=%d",
                settlement_id,
                row["run_id"],
                retry_count,
            )
        elif retry_count >= max_retries:
            await pool.execute(
                """
                UPDATE pending_settlements
                SET status = 'exhausted', retry_count = $2, last_error = $3
                WHERE id = $1
                """,
                settlement_id,
                retry_count,
                error_msg,
            )
            logger.error(
                "Settlement exhausted after %d retries: id=%s run_id=%s error=%s",
                retry_count,
                settlement_id,
                row["run_id"],
                error_msg,
            )
        else:
            # Exponential backoff: 2^retry_count seconds, capped at 300s (5 min)
            backoff_seconds = min(2**retry_count, 300)
            await pool.execute(
                """
                UPDATE pending_settlements
                SET status = 'retrying',
                    retry_count = $2,
                    last_error = $3,
                    next_retry_at = NOW() + ($4 || ' seconds')::INTERVAL
                WHERE id = $1
                """,
                settlement_id,
                retry_count,
                error_msg,
                str(backoff_seconds),
            )

    return processed


async def get_pending_settlements(
    status_filter: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List pending/retrying/exhausted settlements (admin reconciliation)."""
    pool = _get_pool()
    params: list = [limit]
    where = ""
    if status_filter is not None:
        params.append(status_filter)
        where = f"WHERE status = ${len(params)}"
    rows = await pool.fetch(
        f"""
        SELECT id, usage_event_id, org_id, run_id, billing_method,
               amount_usdc, retry_count, max_retries, next_retry_at,
               last_error, status, created_at
        FROM pending_settlements
        {where}
        ORDER BY created_at DESC
        LIMIT $1
        """,
        *params,
    )
    return [dict(r) for r in rows]


async def reset_exhausted_settlement(settlement_id: str) -> bool:
    """Reset an exhausted settlement back to pending (admin manual retry)."""
    pool = _get_pool()
    result = await pool.execute(
        """
        UPDATE pending_settlements
        SET status = 'pending', retry_count = 0, next_retry_at = NOW()
        WHERE id = $1 AND status = 'exhausted'
        """,
        settlement_id,
    )
    return result == "UPDATE 1"


# ─── Spending limits helpers ─────────────────────────────────────────────────


async def get_org_spending_config(org_id: str) -> dict:
    """Return spending config for an org (balance, limit, pause state, daily spend)."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT balance_usdc, spending_limit_usdc, is_paused FROM org_credits WHERE org_id = $1",
        org_id,
    )
    balance = int(row["balance_usdc"]) if row else 0
    spending_limit = int(row["spending_limit_usdc"]) if row else 0
    is_paused = bool(row["is_paused"]) if row else False

    # 24-hour rolling window daily spend from the credit ledger
    daily_row = await pool.fetchrow(
        """
        SELECT COALESCE(SUM(amount_usdc), 0) AS daily_spend
        FROM org_credit_ledger
        WHERE org_id = $1
          AND operation = 'debit'
          AND created_at >= NOW() - INTERVAL '24 hours'
        """,
        org_id,
    )
    daily_spend = int(daily_row["daily_spend"]) if daily_row else 0

    return {
        "org_id": org_id,
        "balance_usdc": balance,
        "spending_limit_usdc": spending_limit,
        "is_paused": is_paused,
        "daily_spend_usdc": daily_spend,
    }


async def update_org_spending_config(
    org_id: str,
    spending_limit_usdc: int | None = None,
    is_paused: bool | None = None,
) -> dict | None:
    """Update spending limit and/or pause state for an org.

    Returns updated config dict, or None if org_credits row doesn't exist.
    """
    pool = _get_pool()
    updates = []
    params: list = [org_id]

    if spending_limit_usdc is not None:
        params.append(spending_limit_usdc)
        updates.append(f"spending_limit_usdc = ${len(params)}")
    if is_paused is not None:
        params.append(is_paused)
        updates.append(f"is_paused = ${len(params)}")

    if not updates:
        return await get_org_spending_config(org_id)

    updates.append("updated_at = NOW()")
    set_clause = ", ".join(updates)

    result = await pool.execute(
        f"UPDATE org_credits SET {set_clause} WHERE org_id = $1",
        *params,
    )
    if result == "UPDATE 0":
        return None
    return await get_org_spending_config(org_id)


# ─── A2A Delegation billing ──────────────────────────────────────────────────


async def check_delegation_budget(org_id: str, estimated_cost_usdc: int) -> str | None:
    """Pre-flight check: can the org afford a delegation of *estimated_cost_usdc*?

    Returns None on success, or an error string describing the shortfall.
    Does NOT debit — that happens after the delegation completes.
    """
    settings = get_settings()
    if not settings.a2a_delegation_billing_enabled:
        return None  # billing disabled — allow unconditionally

    # Apply global cap.
    cap = settings.a2a_delegation_max_cost_usdc
    if estimated_cost_usdc > cap:
        return (
            f"Estimated delegation cost ({estimated_cost_usdc} atomic USDC) "
            f"exceeds global cap ({cap})."
        )

    # Check credit balance.
    balance = await get_credit_balance(org_id)
    if balance < estimated_cost_usdc:
        return (
            f"Insufficient credit for delegation: balance {balance} atomic USDC, "
            f"estimated cost {estimated_cost_usdc}."
        )

    return None


def apply_platform_fee(cost_usdc: int) -> int:
    """Add the platform fee (in basis points) to a raw delegation cost.

    Example: cost=10000, fee=500 bps (5%) → 10500
    """
    settings = get_settings()
    fee_bps = settings.a2a_delegation_platform_fee_bps
    return cost_usdc + (cost_usdc * fee_bps) // 10_000


def get_byok_platform_fee(is_byok: bool) -> int:
    """Return the flat per-run platform fee for BYOK orgs, or 0 for non-BYOK.

    .. deprecated::
        Kept for backward compatibility.  New code should call
        ``calculate_byok_orchestration_cost()`` which uses token-based pricing
        seeded in migration 041.  This function now serves only as a minimum
        floor via ``byok_platform_fee_usdc`` config.
    """
    if not is_byok:
        return 0
    return get_settings().byok_platform_fee_usdc


async def calculate_byok_orchestration_cost(
    tokens_in: int,
    tokens_out: int,
    provider: str = "",
    model: str = "",
) -> int:
    """Calculate the BYOK orchestration fee for a completed run.

    Resolves a BYOK-tier pricing rule (``is_byok=TRUE`` in ``pricing_rules``)
    and bills per-token orchestration overhead.  BYOK users pay the LLM
    provider directly; this fee covers Teardrop's routing, streaming, billing,
    and storage infrastructure.

    Falls back to ``byok_platform_fee_usdc`` (flat floor) when:
    - No BYOK pricing rule exists in the DB yet (pre-migration 041).
    - Computed token cost is less than the configured floor.

    Returns 0 only when both the rule is missing AND the floor is 0.
    """
    settings = get_settings()
    floor = settings.byok_platform_fee_usdc

    rule = await get_live_pricing_for_model(provider, model, is_byok=True)
    if rule is None:
        return floor

    computed = (
        (tokens_in // 1000) * rule.tokens_in_cost_per_1k
        + (tokens_out // 1000) * rule.tokens_out_cost_per_1k
    )

    # Apply floor so zero-token runs (e.g. tool-only or very short prompts) still
    # register as a paid transaction in the ledger.
    return max(computed, floor)


async def fund_delegation(org_id: str, cost_usdc: int, run_id: str, agent_url: str) -> bool:
    """Debit *cost_usdc* from org credit balance for an outbound delegation.

    Wraps ``debit_credit`` with a delegation-specific reason string.
    Returns True on success, False if the debit failed (e.g. insufficient funds).
    """
    reason = f"a2a_delegation run={run_id} agent={agent_url}"
    return await debit_credit(org_id, cost_usdc, reason)


async def record_delegation_event(
    org_id: str,
    run_id: str,
    agent_url: str,
    agent_name: str,
    task_status: str,
    cost_usdc: int,
    billing_method: str = "credit",
    settlement_tx: str = "",
    error: str = "",
) -> None:
    """Insert a row into a2a_delegation_events for audit trail.

    Fire-and-forget: logs but never raises on DB errors so the caller's
    critical path is not interrupted.
    """
    try:
        pool = _get_pool()
        await pool.execute(
            """
            INSERT INTO a2a_delegation_events
                (id, org_id, run_id, agent_url, agent_name,
                 task_status, cost_usdc, billing_method, settlement_tx, error, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
            """,
            str(uuid.uuid4()),
            org_id,
            run_id,
            agent_url,
            agent_name,
            task_status,
            cost_usdc,
            billing_method,
            settlement_tx,
            error,
        )
    except Exception:
        logger.exception(
            "Failed to record delegation event org=%s run=%s agent=%s",
            org_id, run_id, agent_url,
        )


async def get_delegation_events(
    org_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[dict]:
    """Return delegation events for an org (cursor-paginated, newest first)."""
    pool = _get_pool()
    if cursor is None:
        rows = await pool.fetch(
            """
            SELECT id, org_id, run_id, agent_url, agent_name,
                   task_status, cost_usdc, billing_method, settlement_tx, error, created_at
            FROM a2a_delegation_events
            WHERE org_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            org_id,
            limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, org_id, run_id, agent_url, agent_name,
                   task_status, cost_usdc, billing_method, settlement_tx, error, created_at
            FROM a2a_delegation_events
            WHERE org_id = $1 AND created_at < $3
            ORDER BY created_at DESC
            LIMIT $2
            """,
            org_id,
            limit,
            cursor,
        )
    return [dict(r) for r in rows]


def get_treasury_signer():
    """Return an x402 EthAccountSigner backed by the treasury private key.

    Raises RuntimeError if the key is not configured.
    """
    settings = get_settings()
    if not settings.x402_treasury_private_key:
        raise RuntimeError(
            "x402_treasury_private_key is not configured — "
            "cannot sign outbound x402 delegation payments"
        )

    from eth_account import Account
    from x402.mechanisms.evm import EthAccountSigner

    account = Account.from_key(settings.x402_treasury_private_key)
    return EthAccountSigner(account)
