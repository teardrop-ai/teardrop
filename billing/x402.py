# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""x402 verification/settlement and USDC top-up flows."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import sentry_sdk

from billing.context import _bind_pool, _clear_pool, _get_pool, _has_pool, _reset_daily_spend_caches
from billing.models import BillingResult, atomic_usdc_to_price_str
from billing.pricing import get_current_pricing, get_live_pricing, reset_pricing_caches
from shared.db_pool import PgPool
from teardrop.a2a_client import async_validate_url
from teardrop.config import get_settings

logger = logging.getLogger(__name__)


# Lazy x402 imports (only when billing is enabled)
_server = None  # x402ResourceServer instance
_servers: list = []
_facilitator_failures: list[int] = []
_facilitator_unhealthy_until: list[float] = []
_requirements_cache: list | None = None
_exact_requirements_cache: list | None = None
_upto_requirements_cache: list | None = None
_last_requirements_price_usdc: int = -1
_last_requirements_topology: tuple[tuple[str, ...], tuple[str, ...]] | None = None


def _get_server():
    """Return the initialized x402ResourceServer, or raise if not ready."""
    if _server is None:
        raise RuntimeError("Billing not initialised — call init_billing() first")
    return _server


def _get_servers() -> list:
    return _servers or [_get_server()]


def _ensure_facilitator_health_state(server_count: int) -> None:
    if len(_facilitator_failures) != server_count:
        _facilitator_failures[:] = [0] * server_count
    if len(_facilitator_unhealthy_until) != server_count:
        _facilitator_unhealthy_until[:] = [0.0] * server_count


def _effective_facilitator_urls(settings) -> list[str]:
    configured = getattr(settings, "x402_facilitator_urls", None)
    return configured if isinstance(configured, list) and configured else [settings.x402_facilitator_url]


def _effective_treasury_addresses(settings) -> list[str]:
    configured = getattr(settings, "x402_treasury_addresses", None)
    if isinstance(configured, list) and configured:
        return configured
    return [settings.x402_pay_to_address] if settings.x402_pay_to_address else []


def _facilitator_host(url: str) -> str:
    return urlsplit(url).hostname or "unknown"


def _build_requirements(server, settings, treasuries: list[str], price: str) -> tuple[list, list | None, list]:
    from x402 import ResourceConfig

    exact = []
    upto = []
    for treasury in treasuries:
        exact.extend(
            server.build_payment_requirements(
                ResourceConfig(scheme="exact", network=settings.x402_network, pay_to=treasury, price=price)
            )
        )
        if settings.x402_scheme == "upto":
            upto.extend(
                server.build_payment_requirements(
                    ResourceConfig(
                        scheme="upto",
                        network=settings.x402_network,
                        pay_to=treasury,
                        price=settings.x402_upto_max_amount,
                    )
                )
            )
    return exact, upto or None, [*upto, *exact]


async def init_billing(pool: PgPool) -> None:
    """Initialise x402 resource server and cache payment requirements."""
    global _server, _servers, _facilitator_failures, _facilitator_unhealthy_until
    global _requirements_cache, _last_requirements_price_usdc, _last_requirements_topology
    global _exact_requirements_cache, _upto_requirements_cache

    settings = get_settings()

    # Always store pool — pricing queries run regardless of billing_enabled.
    _bind_pool(pool)

    if not settings.billing_enabled:
        logger.info("Billing disabled — skipping x402 initialisation")
        return

    facilitator_urls = _effective_facilitator_urls(settings)
    treasuries = _effective_treasury_addresses(settings)
    if not treasuries:
        raise RuntimeError("billing_enabled=True but no x402 treasury address is configured")

    from x402 import x402ResourceServer
    from x402.http import HTTPFacilitatorClient
    from x402.http.facilitator_client_base import FacilitatorConfig
    from x402.mechanisms.evm.exact import ExactEvmServerScheme

    upto_scheme = None
    if settings.x402_scheme == "upto":
        try:
            from x402.mechanisms.evm.upto import UptoEvmServerScheme  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "x402 upto scheme is not available in the installed package. Upgrade: pip install 'x402[fastapi,evm]>=2.8.0'"
            ) from exc
        upto_scheme = UptoEvmServerScheme

    initialized_servers = []
    for facilitator_url in facilitator_urls:
        if await async_validate_url(facilitator_url) is not None:
            logger.warning("x402 facilitator blocked by URL policy host=%s", _facilitator_host(facilitator_url))
            continue
        try:
            facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=facilitator_url))
            server = x402ResourceServer(facilitator)
            server.register(settings.x402_network, ExactEvmServerScheme())
            if upto_scheme is not None:
                server.register(settings.x402_network, upto_scheme())
            server.initialize()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "x402 facilitator initialization failed host=%s error_type=%s",
                _facilitator_host(facilitator_url),
                type(exc).__name__,
            )
            continue
        initialized_servers.append(server)

    if not initialized_servers:
        raise RuntimeError("No x402 facilitator could be initialized")
    _servers = initialized_servers
    _server = initialized_servers[0]
    _facilitator_failures = [0] * len(_servers)
    _facilitator_unhealthy_until = [0.0] * len(_servers)

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
                    "Pricing DB query failed on startup after retry: %s — using config fallback price=%s",
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

    _exact_requirements_cache, _upto_requirements_cache, _requirements_cache = _build_requirements(
        _server, settings, treasuries, price_str
    )
    _last_requirements_topology = (tuple(treasuries), tuple(facilitator_urls))

    advertised_price = settings.x402_upto_max_amount if settings.x402_scheme == "upto" else price_str
    logger.info(
        "Billing initialised: network=%s pay_to=%s price=%s scheme=%s",
        settings.x402_network,
        ",".join(treasuries),
        advertised_price,
        settings.x402_scheme,
    )

    # Operational calibration check: the x402 "exact" scheme settles the signed
    # authorization amount (run_price_usdc) and cannot settle a different per-tool
    # cost. If a tool's flat call cost exceeds run_price_usdc, exact-scheme MCP
    # calls undercharge by the delta. Warn so operators raise run_price_usdc or
    # switch the MCP endpoint to the "upto" scheme.
    if settings.x402_scheme == "exact" and rule is not None and rule.tool_call_cost > rule.run_price_usdc:
        logger.warning(
            "x402 exact scheme: tool_call_cost=%d exceeds run_price_usdc=%d — "
            "MCP tool calls priced above run_price_usdc will undercharge by the "
            "delta. Raise run_price_usdc or set x402_scheme=upto.",
            rule.tool_call_cost,
            rule.run_price_usdc,
        )


async def close_billing() -> None:
    """Release billing resources."""
    global _server, _servers, _facilitator_failures, _facilitator_unhealthy_until
    global _requirements_cache, _last_requirements_price_usdc, _last_requirements_topology
    global _exact_requirements_cache, _upto_requirements_cache

    _server = None
    _servers = []
    _facilitator_failures = []
    _facilitator_unhealthy_until = []
    _requirements_cache = None
    _exact_requirements_cache = None
    _upto_requirements_cache = None
    _last_requirements_price_usdc = -1
    _last_requirements_topology = None

    reset_pricing_caches()
    _reset_daily_spend_caches()
    _clear_pool()

    logger.info("Billing resources released")


def get_payment_requirements() -> list:
    """Return cached PaymentRequirements for /agent/run."""
    if _requirements_cache is None:
        raise RuntimeError("Billing not initialised or disabled")
    return _requirements_cache


def _serialize_requirement(requirement: Any) -> dict[str, Any]:
    if hasattr(requirement, "model_dump"):
        return requirement.model_dump(by_alias=True, exclude_none=True)
    if isinstance(requirement, Mapping):
        return dict(requirement)
    return dict(vars(requirement))


def _build_payment_required(
    *,
    error: str | None = "Payment required",
    resource: Mapping[str, Any] | None = None,
    extensions: dict[str, Any] | None = None,
    requirements: list | None = None,
):
    from x402.schemas.payments import PaymentRequired, PaymentRequirements, ResourceInfo

    resource_info = None
    if resource is not None:
        resource_payload = dict(resource)
        if "mime_type" in resource_payload and "mimeType" not in resource_payload:
            resource_payload["mimeType"] = resource_payload.pop("mime_type")
        resource_info = ResourceInfo.model_validate(resource_payload)

    source_requirements = get_payment_requirements() if requirements is None else requirements
    requirements = [
        req if isinstance(req, PaymentRequirements) else PaymentRequirements.model_validate(_serialize_requirement(req))
        for req in source_requirements
    ]

    return PaymentRequired(
        error=error,
        resource=resource_info,
        accepts=requirements,
        extensions=extensions or None,
    )


def build_402_response_body(
    error: str | None = "Payment required",
    resource: Mapping[str, Any] | None = None,
    extensions: dict[str, Any] | None = None,
    requirements: list | None = None,
) -> dict[str, Any]:
    """Build the JSON body for a 402 Payment Required response."""
    return _build_payment_required(
        error=error,
        resource=resource,
        extensions=extensions,
        requirements=requirements,
    ).model_dump(by_alias=True, exclude_none=True)


def build_402_headers(
    error: str | None = "Payment required",
    resource: Mapping[str, Any] | None = None,
    extensions: dict[str, Any] | None = None,
    requirements: list | None = None,
) -> dict[str, str]:
    """Build response headers for a 402 Payment Required response."""
    import base64

    from x402.http import PAYMENT_REQUIRED_HEADER, encode_payment_required_header

    payment_required = _build_payment_required(
        error=error,
        resource=resource,
        extensions=extensions,
        requirements=requirements,
    )
    legacy_serialised = json.dumps(
        [req.model_dump(by_alias=True, exclude_none=True) for req in payment_required.accepts],
        default=str,
    )
    legacy_encoded = base64.b64encode(legacy_serialised.encode()).decode()
    return {
        PAYMENT_REQUIRED_HEADER: encode_payment_required_header(payment_required),
        "X-PAYMENT-REQUIRED": legacy_encoded,
    }


async def _rebuild_requirements_if_stale() -> None:
    """Rebuild x402 payment requirements when the DB pricing rule has changed."""
    global _requirements_cache, _last_requirements_price_usdc, _last_requirements_topology
    global _exact_requirements_cache, _upto_requirements_cache

    if _server is None:
        return

    rule = await get_live_pricing()
    if rule is None:
        return

    settings = get_settings()
    treasuries = _effective_treasury_addresses(settings)
    topology = (tuple(treasuries), tuple(_effective_facilitator_urls(settings)))
    if rule.run_price_usdc == _last_requirements_price_usdc and topology == _last_requirements_topology:
        return
    if not treasuries:
        logger.error("Payment requirements cannot be rebuilt without a treasury address")
        return

    new_price_str = atomic_usdc_to_price_str(rule.run_price_usdc)

    _exact_requirements_cache, _upto_requirements_cache, _requirements_cache = _build_requirements(
        _server, settings, treasuries, new_price_str
    )

    _last_requirements_price_usdc = rule.run_price_usdc
    _last_requirements_topology = topology
    logger.info(
        "Payment requirements updated: run_price_usdc=%d price=%s",
        rule.run_price_usdc,
        new_price_str,
    )


async def _claim_payment_nonce(payment_header: str) -> bool:
    """Atomically claim a payment header to block concurrent replays.

    Returns True if this is the first time the header is seen (caller may
    proceed), False if it was already claimed (reject).

    The claim key is the SHA-256 of the raw header. On-chain settlement already
    prevents an EIP-3009 authorization from spending twice; this guard closes
    the narrower concurrent window where two in-flight requests with the same
    header both verify and execute a paid tool before either settles. If the
    nonce store is unavailable we fail open (log + allow) so a transient DB
    issue cannot take down all paid traffic — the chain remains the final
    double-spend backstop.
    """
    if not _has_pool():
        logger.warning("x402 nonce store unavailable (no pool); skipping replay claim")
        return True

    nonce_hash = hashlib.sha256(payment_header.encode("utf-8")).hexdigest()
    pool = _get_pool()
    try:
        claimed = await pool.fetchval(
            """
            INSERT INTO x402_payment_nonces (nonce_hash)
            VALUES ($1)
            ON CONFLICT (nonce_hash) DO NOTHING
            RETURNING nonce_hash
            """,
            nonce_hash,
        )
    except Exception:
        logger.warning("x402 nonce claim query failed; failing open", exc_info=True)
        return True
    return claimed is not None


async def release_payment_nonce(payment_header: str) -> None:
    """Release a local replay claim when verification never reaches settlement."""
    if not _has_pool():
        return
    nonce_hash = hashlib.sha256(payment_header.encode("utf-8")).hexdigest()
    await _get_pool().execute(
        "DELETE FROM x402_payment_nonces WHERE nonce_hash = $1",
        nonce_hash,
    )


async def cleanup_expired_payment_nonces(retention_hours: int = 24) -> int:
    """Delete payment-nonce claims older than ``retention_hours``.

    A signed x402 authorization is short-lived (validBefore window); once it is
    well past use it can never be replayed against the chain, so its claim row
    is safe to drop. Bounds table growth. Returns the number of rows deleted.
    """
    if not _has_pool():
        return 0
    pool = _get_pool()
    result = await pool.execute(
        "DELETE FROM x402_payment_nonces WHERE claimed_at < NOW() - make_interval(hours => $1)",
        retention_hours,
    )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def verify_payment(payment_header: str, requirements: list | None = None) -> BillingResult:
    """Verify a payment header against cached requirements."""
    await _rebuild_requirements_if_stale()

    from x402 import parse_payment_payload

    servers = _get_servers()
    _ensure_facilitator_health_state(len(servers))
    reqs = get_payment_requirements() if requirements is None else requirements

    if not reqs:
        return BillingResult(error="No payment requirements configured")

    try:
        import base64 as _base64

        payload = parse_payment_payload(_base64.b64decode(payment_header))
    except Exception as exc:
        logger.warning("Failed to parse payment header error_type=%s", type(exc).__name__)
        return BillingResult(error="Malformed payment header")

    last_error = "No payment requirements matched"
    for requirement in reqs:
        result = None
        facilitator_index = 0
        now = time.monotonic()
        available = [index for index in range(len(servers)) if _facilitator_unhealthy_until[index] <= now]
        if not available:
            available = [min(range(len(servers)), key=_facilitator_unhealthy_until.__getitem__)]
        for index in available:
            try:
                result = await servers[index].verify_payment(payload, requirement)
            except Exception as exc:  # noqa: BLE001
                _facilitator_failures[index] += 1
                cooldown = min(2 ** (_facilitator_failures[index] - 1) * 5, 300)
                _facilitator_unhealthy_until[index] = time.monotonic() + cooldown
                logger.warning(
                    "x402 facilitator verification unavailable index=%d error_type=%s cooldown_seconds=%d",
                    index,
                    type(exc).__name__,
                    cooldown,
                )
                last_error = "Payment verification service unavailable"
                continue
            _facilitator_failures[index] = 0
            _facilitator_unhealthy_until[index] = 0.0
            facilitator_index = index
            break
        if result is None:
            continue

        if result.is_valid:
            detected_scheme = getattr(requirement, "scheme", "exact") or "exact"
            # Atomic concurrent-replay guard: claim the payment header before
            # returning verified=True so two in-flight requests carrying the
            # same signed header cannot both proceed to execute a paid tool.
            if not await _claim_payment_nonce(payment_header):
                logger.warning("x402 payment header already claimed (concurrent replay rejected)")
                return BillingResult(error="Payment already used. Sign a new payment authorization.")
            payer = result.payer if isinstance(result.payer, str) else ""
            return BillingResult(
                verified=True,
                payment_payload=payload,
                payment_requirements=requirement,
                scheme=detected_scheme,
                payer=payer,
                facilitator_index=facilitator_index,
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

    servers = _get_servers()
    if billing_result.facilitator_index >= len(servers):
        billing_result.error = "Payment settlement route is unavailable"
        return billing_result
    server = servers[billing_result.facilitator_index]

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
        logger.error("Payment settlement unavailable error_type=%s", type(exc).__name__)
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("rail", "x402")
            scope.set_tag("error_type", type(exc).__name__)
            sentry_sdk.capture_message("x402 payment settlement unavailable", level="error")
        billing_result.error = "Payment settlement service unavailable"
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
    requirements = []
    for treasury in _effective_treasury_addresses(settings):
        config = ResourceConfig(
            scheme=settings.x402_scheme,
            network=settings.x402_network,
            pay_to=treasury,
            price=atomic_usdc_to_price_str(amount_usdc),
        )
        requirements.extend(server.build_payment_requirements(config))
    return requirements


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
        logger.warning("usdc_topup: failed to parse payment header error_type=%s", type(exc).__name__)
        return BillingResult(error="Malformed payment header")

    requirement = requirements[0]

    try:
        verify_result = await server.verify_payment(payload, requirement)
    except Exception as exc:
        logger.error("usdc_topup: verification unavailable error_type=%s", type(exc).__name__)
        return BillingResult(error="Payment verification service unavailable")

    if not verify_result.is_valid:
        reason = verify_result.invalid_reason or verify_result.invalid_message or "invalid signature or amount"
        logger.warning("usdc_topup: verification failed: %s", reason)
        return BillingResult(error=f"Payment verification failed: {reason}")

    try:
        settle_result = await server.settle_payment(payload, requirement)
    except Exception as exc:
        logger.error("usdc_topup: settlement unavailable error_type=%s", type(exc).__name__)
        return BillingResult(error="Payment settlement service unavailable")

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
