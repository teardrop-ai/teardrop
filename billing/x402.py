# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""x402 verification/settlement and USDC top-up flows."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import asyncpg
import sentry_sdk

from billing.context import _bind_pool, _clear_pool, _get_pool, _reset_daily_spend_caches
from billing.models import BillingResult, atomic_usdc_to_price_str
from billing.pricing import get_current_pricing, get_live_pricing, reset_pricing_caches
from teardrop.config import get_settings

logger = logging.getLogger(__name__)


# Lazy x402 imports (only when billing is enabled)
_server = None  # x402ResourceServer instance
_requirements_cache: list | None = None
_exact_requirements_cache: list | None = None
_upto_requirements_cache: list | None = None
_last_requirements_price_usdc: int = -1


def _get_server():
    """Return the initialized x402ResourceServer, or raise if not ready."""
    if _server is None:
        raise RuntimeError("Billing not initialised — call init_billing() first")
    return _server


async def init_billing(pool: asyncpg.Pool) -> None:
    """Initialise x402 resource server and cache payment requirements."""
    global _server, _requirements_cache, _last_requirements_price_usdc
    global _exact_requirements_cache, _upto_requirements_cache

    settings = get_settings()

    # Always store pool — pricing queries run regardless of billing_enabled.
    _bind_pool(pool)

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
                "x402 upto scheme is not available in the installed package. Upgrade: pip install 'x402[fastapi,evm]>=2.8.0'"
            ) from exc

        server.register(settings.x402_network, UptoEvmServerScheme())

    server.initialize()
    _server = server

    # Resolve price from live pricing_rules; fall back to config value.
    # Use get_current_pricing() directly (bypassing the TTL cache) so that a
    # transient DB error at startup — which TTLCache silently converts to None
    # via stale_default — triggers a retry instead of a silent fallback.
    rule: object = None
    for attempt in range(2):
        try:
            rule = await get_current_pricing()
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                logger.warning("Pricing DB query failed on startup (attempt 1); retrying: %s", exc)
                await asyncio.sleep(1)
            else:
                logger.error(
                    "Pricing DB query failed on startup after retry: %s — "
                    "using config fallback price=%s",
                    exc,
                    settings.x402_run_price,
                )
    if rule is not None:
        price_str = atomic_usdc_to_price_str(rule.run_price_usdc)
        _last_requirements_price_usdc = rule.run_price_usdc
    else:
        price_str = settings.x402_run_price
        logger.error(
            "No global pricing_rules row found (provider='', model='', is_byok=FALSE); "
            "using config fallback price=%s — run migrations to seed pricing data",
            price_str,
        )

    # Always build exact requirements.
    exact_config = ResourceConfig(
        scheme="exact",
        network=settings.x402_network,
        pay_to=settings.x402_pay_to_address,
        price=price_str,
    )
    _exact_requirements_cache = server.build_payment_requirements(exact_config)

    if settings.x402_scheme == "upto":
        upto_config = ResourceConfig(
            scheme="upto",
            network=settings.x402_network,
            pay_to=settings.x402_pay_to_address,
            price=settings.x402_upto_max_amount,
        )
        _upto_requirements_cache = server.build_payment_requirements(upto_config)
        _requirements_cache = [*_upto_requirements_cache, *_exact_requirements_cache]
    else:
        _upto_requirements_cache = None
        _requirements_cache = list(_exact_requirements_cache)

    advertised_price = settings.x402_upto_max_amount if settings.x402_scheme == "upto" else price_str
    logger.info(
        "Billing initialised: network=%s pay_to=%s price=%s scheme=%s",
        settings.x402_network,
        settings.x402_pay_to_address,
        advertised_price,
        settings.x402_scheme,
    )


async def close_billing() -> None:
    """Release billing resources."""
    global _server, _requirements_cache, _last_requirements_price_usdc
    global _exact_requirements_cache, _upto_requirements_cache

    _server = None
    _requirements_cache = None
    _exact_requirements_cache = None
    _upto_requirements_cache = None
    _last_requirements_price_usdc = -1

    reset_pricing_caches()
    _reset_daily_spend_caches()
    _clear_pool()

    logger.info("Billing resources released")


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
    """Rebuild x402 payment requirements when the DB pricing rule has changed."""
    global _requirements_cache, _last_requirements_price_usdc
    global _exact_requirements_cache, _upto_requirements_cache

    if _server is None:
        return

    rule = await get_live_pricing()
    if rule is None:
        return

    if rule.run_price_usdc == _last_requirements_price_usdc:
        return

    settings = get_settings()
    from x402 import ResourceConfig

    new_price_str = atomic_usdc_to_price_str(rule.run_price_usdc)

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
    """Verify a payment header against cached requirements."""
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

    last_error = "No payment requirements matched"
    for requirement in reqs:
        try:
            result = await server.verify_payment(payload, requirement)
        except Exception as exc:
            logger.debug("Verification attempt failed for scheme=%s: %s", getattr(requirement, "scheme", "?"), exc)
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
        logger.debug(
            "Payment verification failed for scheme=%s: %s (payer=%s)", getattr(requirement, "scheme", "?"), reason, result.payer
        )
        last_error = f"Payment verification failed: {reason}"

    logger.warning("All payment requirements failed verification: %s", last_error)
    return BillingResult(error=last_error)


async def settle_payment(
    billing_result: BillingResult,
    actual_cost_usdc: int | None = None,
) -> BillingResult:
    """Settle a verified payment on-chain via the facilitator."""
    if not billing_result.verified:
        billing_result.error = "Cannot settle unverified payment"
        return billing_result

    server = _get_server()

    try:
        if billing_result.scheme == "upto" and actual_cost_usdc is not None:
            actual_cost_usdc = max(0, actual_cost_usdc)
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
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("rail", "x402")
            sentry_sdk.capture_exception(exc)
        billing_result.error = f"Settlement failed: {exc}"
        return billing_result

    if not result.success:
        billing_result.error = "Settlement rejected by facilitator"
        return billing_result

    billing_result.settled = True
    billing_result.tx_hash = result.transaction or ""
    if not billing_result.tx_hash:
        logger.warning(
            "settle_payment: facilitator returned success without tx hash scheme=%s",
            billing_result.scheme,
        )

    if billing_result.scheme == "upto" and actual_cost_usdc is not None:
        billing_result.amount_usdc = actual_cost_usdc
    else:
        req = billing_result.payment_requirements
        billing_result.amount_usdc = int(getattr(req, "amount", "0") or "0")

    return billing_result


def build_usdc_topup_requirements(amount_usdc: int) -> list:
    """Build x402 PaymentRequirements for a USDC on-chain top-up."""
    from x402 import ResourceConfig  # noqa: PLC0415

    settings = get_settings()
    server = _get_server()
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
    """Verify and immediately settle a USDC top-up payment header on-chain."""
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
        reason = verify_result.invalid_reason or verify_result.invalid_message or "invalid signature or amount"
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

    tx_hash = settle_result.transaction or ""
    logger.info("usdc_topup: settled tx_hash=%s amount_usdc=%s", tx_hash, amount_usdc)
    return BillingResult(verified=True, settled=True, tx_hash=tx_hash, amount_usdc=amount_usdc)


async def credit_usdc_topup(org_id: str, amount_usdc: int, tx_hash: str) -> int | None:
    """Credit amount_usdc to org's balance after a confirmed on-chain top-up."""
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


def get_treasury_signer():
    """Return an x402 EthAccountSigner backed by the treasury private key."""
    settings = get_settings()
    if not settings.x402_treasury_private_key:
        raise RuntimeError("x402_treasury_private_key is not configured — cannot sign outbound x402 delegation payments")

    from eth_account import Account
    from x402.mechanisms.evm import EthAccountSigner

    account = Account.from_key(settings.x402_treasury_private_key)
    return EthAccountSigner(account)
