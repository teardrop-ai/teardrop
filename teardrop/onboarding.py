# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""x402 payment-first org bootstrap (zero-human onboarding).

An external agent that has never authenticated can POST ``grant_type=x402`` to
``/token`` with a signed x402 payment header. The payment is verified
pre-authentication, the payer's EVM address becomes the wallet identity, and a
new machine-provisioned org + user + client credential is returned.

Security contract:
  * Identity comes ONLY from the facilitator-verified payer address; an empty
    payer fails closed.
    * Human-owned wallets cannot be converted through x402; they receive 409 and
        must use SIWE. Existing machine-provisioned orgs can accept a new payment
        into the same org, so a failed first settlement does not orphan the payer.
    * Provisioning happens BEFORE settlement; a failed settle leaves the org with
        a zero balance and an audited failed outcome, never a credited-but-unpaid org.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from decimal import Decimal, InvalidOperation

import sentry_sdk
from fastapi import HTTPException, Request, status
from web3 import Web3

import billing
from teardrop.auth import create_access_token
from teardrop.config import get_settings
from teardrop.rate_limit import _enforce_rate_limit
from teardrop.users import get_org_by_id
from teardrop.wallets import (
    get_provisioning_state_by_payment_ref,
    get_wallet_by_address_any_chain,
    provision_org_for_wallet,
)

logger = logging.getLogger(__name__)
settings = get_settings()

__all__ = ["bootstrap_org_from_payment", "get_bootstrap_payment_requirements"]


def _price_str_to_atomic(price_str: str) -> int:
    """Convert a dollar price string to atomic USDC without float rounding."""
    try:
        atomic = Decimal(price_str.strip().lstrip("$")) * Decimal(1_000_000)
        if atomic != atomic.to_integral_value() or atomic < 0:
            return 0
        return int(atomic)
    except (InvalidOperation, ValueError, AttributeError):
        return 0


def _requirement_amount_usdc(requirement: object) -> int:
    """Read the x402 smallest-unit amount as a positive integer."""
    raw_amount = getattr(requirement, "amount", None)
    if isinstance(raw_amount, int):
        amount = raw_amount
    elif isinstance(raw_amount, str) and raw_amount.isdecimal():
        amount = int(raw_amount)
    else:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Onboarding pricing is unavailable")
    if amount <= 0:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Onboarding pricing is unavailable")
    return amount


async def get_bootstrap_payment_requirements() -> tuple[int, list]:
    """Return the amount and x402 requirements needed to fund one credit run."""
    pricing = await billing.get_live_pricing()
    live_run_price = pricing.run_price_usdc if pricing is not None else _price_str_to_atomic(settings.x402_run_price)
    amount_usdc = max(live_run_price, settings.credit_min_run_reserve_usdc)
    if amount_usdc <= 0:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Onboarding pricing is unavailable")
    try:
        requirements = billing.build_usdc_topup_requirements(amount_usdc)
    except RuntimeError:
        logger.warning("x402 onboarding requirements unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Onboarding payment is temporarily unavailable",
        )
    if not requirements:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Onboarding pricing is unavailable")
    return amount_usdc, requirements


async def bootstrap_org_from_payment(payment_header: str, request: Request) -> dict:
    """Verify an x402 payment and provision a fresh machine org for its payer."""
    amount_usdc, requirements = await get_bootstrap_payment_requirements()
    nonce_hash = hashlib.sha256(payment_header.encode("utf-8")).hexdigest()
    payment_ref = f"x402:{nonce_hash}"

    recovery_state = await _safe_get_recovery_state(payment_ref)
    recovered = await _recover_credit_failed_bootstrap(recovery_state, amount_usdc, payment_ref)
    if recovered is not None:
        return recovered

    billing_result = await billing.verify_payment(payment_header, requirements=requirements)
    if not billing_result.verified:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment verification failed",
        )
    try:
        verified_amount = _requirement_amount_usdc(billing_result.payment_requirements)
    except HTTPException:
        await _release_unsettled_claim(payment_header)
        raise
    if verified_amount != amount_usdc:
        await _release_unsettled_claim(payment_header)
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment amount does not match onboarding requirements",
        )

    payer_raw = billing_result.payer if isinstance(billing_result.payer, str) else ""
    if not payer_raw:
        await _release_unsettled_claim(payment_header)
        logger.warning("x402 onboarding rejected: facilitator did not report a payer address")
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment verified but payer identity unavailable",
        )
    try:
        address = Web3.to_checksum_address(payer_raw)
    except (TypeError, ValueError):
        await _release_unsettled_claim(payment_header)
        logger.warning("x402 onboarding rejected: invalid facilitator payer identity")
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment verified but payer identity unavailable",
        )

    try:
        chain_id = _chain_id_from_network(settings.x402_network)
    except ValueError:
        await _release_unsettled_claim(payment_header)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="x402 network is misconfigured")

    existing_wallet = await get_wallet_by_address_any_chain(address)
    existing_machine_org = False
    if existing_wallet is not None:
        existing_org = await get_org_by_id(existing_wallet.org_id)
        existing_machine_org = bool(existing_org and existing_org.acquisition_source in {"siwe", "x402"})
        recovery_state = await _safe_get_recovery_state(payment_ref)
        initial = recovery_state["provisioned"] if recovery_state else None
        latest = recovery_state["latest_settlement"] if recovery_state else None
        if not existing_machine_org or (
            recovery_state is not None
            and (
                initial is None
                or initial["org_id"] != existing_wallet.org_id
                or initial["method"] != "x402"
                or initial["chain_id"] != chain_id
                or initial["payer_address"].lower() != address.lower()
                or int(initial["amount_usdc"]) != amount_usdc
                or latest is None
                or latest["settlement_status"] != "failed"
            )
        ):
            if not existing_machine_org or recovery_state is None:
                await _release_unsettled_claim(payment_header)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This wallet is already registered. Sign in via SIWE instead.",
            )

    if existing_wallet is None:
        # First-time org creation is free of an economic top-up history, so
        # throttle it by verified payer address. Repeat top-ups into an
        # existing machine org are already economically gated and skip this.
        try:
            await _enforce_rate_limit(
                f"provision:addr:{address.lower()}",
                settings.rate_limit_org_provision_rpm,
                detail="Too many new org provisioning attempts for this wallet. Please try again later.",
            )
        except HTTPException:
            await _release_unsettled_claim(payment_header)
            raise

    try:
        provisioned = await provision_org_for_wallet(
            address,
            chain_id,
            acquisition_source="x402",
            payment_ref=payment_ref,
            amount_usdc=amount_usdc,
        )
    except Exception:
        await _release_unsettled_claim(payment_header)
        raise
    if not provisioned.created and recovery_state is None and not existing_machine_org:
        await _release_unsettled_claim(payment_header)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This wallet is already registered. Sign in via SIWE instead.",
        )
    org = provisioned.org

    settled_tx = ""
    settlement_error = ""
    settle_result = None
    settlement_ambiguous = False
    try:
        settle_result = await billing.settle_payment(billing_result)
        if not settle_result.settled:
            settlement_error = settle_result.error or "settlement outcome unavailable"
            settlement_ambiguous = settlement_error != "Settlement rejected by facilitator"
            logger.warning("x402 onboarding settlement failed for org=%s", org.id)
        else:
            settled_tx = settle_result.tx_hash
    except Exception as exc:
        settlement_error = "settlement request failed"
        settlement_ambiguous = True
        logger.error("x402 onboarding settlement error for org=%s: %s", org.id, type(exc).__name__)
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("rail", "x402")
            scope.set_tag("flow", "onboarding")
            sentry_sdk.capture_exception(exc)

    if (
        settle_result is None
        or not settle_result.settled
        or not settle_result.tx_hash
        or settle_result.amount_usdc != amount_usdc
    ):
        if not settlement_error:
            settlement_error = "settlement response was incomplete"
        audit_recorded = await _record_settlement_event(
            org.id,
            address,
            chain_id,
            nonce_hash,
            amount_usdc,
            settled_tx,
            "ambiguous" if settlement_ambiguous else "failed",
            settlement_error,
        )
        if audit_recorded is not False and not settlement_ambiguous:
            await _release_unsettled_claim(payment_header)
        if audit_recorded is False:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Payment outcome is temporarily unavailable; no account credit was issued",
            )
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment could not be settled; no account credit was issued",
        )

    try:
        credential, client_secret = await billing.record_onboarding_settlement(
            org.id,
            amount_usdc,
            payment_ref,
            address,
            chain_id,
            settled_tx,
        )
    except Exception:
        logger.exception("x402 onboarding credit transaction failed org=%s", org.id)
        audit_recorded = await _record_settlement_event(
            org.id,
            address,
            chain_id,
            nonce_hash,
            amount_usdc,
            settled_tx,
            "credit_failed",
            "credit ledger update failed",
        )
        if audit_recorded is False:
            logger.error("x402 onboarding credit failure audit unavailable org=%s", org.id)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Account credit is temporarily unavailable")

    return await _issue_bootstrap_credential(org.id, credential, client_secret)


async def _issue_bootstrap_credential(org_id: str, cred: object, client_secret: str | None) -> dict:
    access_token = create_access_token(
        subject=cred.client_id,
        extra_claims={"org_id": org_id, "auth_method": "client_credentials", "role": "user"},
    )
    logger.info(
        "x402 onboarding bootstrapped org=%s credential_reused=%s payer=%s tx=%s",
        org_id,
        client_secret is None,
        "redacted",
        "redacted",
    )
    response = {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.jwt_access_token_expire_minutes * 60,
        "org_id": org_id,
        "client_id": cred.client_id,
    }
    if client_secret is not None:
        response["client_secret"] = client_secret
    return response


async def _safe_get_recovery_state(payment_ref: str) -> dict | None:
    try:
        return await get_provisioning_state_by_payment_ref(payment_ref)
    except Exception:
        logger.warning("x402 onboarding recovery lookup unavailable", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Onboarding state is temporarily unavailable",
        )


async def _recover_credit_failed_bootstrap(
    recovery_state: dict | None,
    amount_usdc: int,
    payment_ref: str,
) -> dict | None:
    if not recovery_state:
        return None
    initial = recovery_state.get("provisioned") or {}
    latest = recovery_state.get("latest_settlement") or {}
    try:
        expected_chain_id = _chain_id_from_network(settings.x402_network)
        initial_amount = int(initial.get("amount_usdc", 0))
    except (TypeError, ValueError):
        return None
    if (
        initial.get("method") != "x402"
        or latest.get("settlement_status") != "credit_failed"
        or not latest.get("settlement_tx")
        or initial.get("payment_ref") != payment_ref
        or initial.get("chain_id") != expected_chain_id
        or initial_amount != amount_usdc
    ):
        return None
    try:
        chain_id = _chain_id_from_network(settings.x402_network)
        credential, client_secret = await billing.record_onboarding_settlement(
            initial["org_id"],
            amount_usdc,
            payment_ref,
            initial["payer_address"],
            chain_id,
            latest["settlement_tx"],
        )
    except Exception:
        logger.exception("x402 onboarding recovery credit transaction failed org=%s", initial.get("org_id"))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Account credit is temporarily unavailable")
    return await _issue_bootstrap_credential(initial["org_id"], credential, client_secret)


async def _record_settlement_event(
    org_id: str,
    payer_address: str,
    chain_id: int,
    nonce_hash: str,
    amount_usdc: int,
    settled_tx: str,
    settlement_status: str,
    settlement_error: str,
) -> bool:
    try:
        pool = billing._get_pool()
        await pool.execute(
            """
            INSERT INTO org_provisioning_events
                (id, org_id, method, payer_address, chain_id, settlement_tx,
                 payment_ref, amount_usdc, event_type, settlement_status, settlement_error)
            VALUES ($1, $2, 'x402', $3, $4, $5, $6, $7, 'settlement', $8, $9)
            """,
            str(uuid.uuid4()),
            org_id,
            payer_address,
            chain_id,
            settled_tx,
            f"x402:{nonce_hash}",
            amount_usdc,
            settlement_status,
            settlement_error,
        )
        return True
    except Exception:
        logger.exception("settlement audit insert failed org=%s", org_id)
        return False


async def _release_unsettled_claim(payment_header: str) -> None:
    try:
        await billing.release_payment_nonce(payment_header)
    except Exception:
        logger.warning("x402 onboarding replay claim release failed", exc_info=True)


def _chain_id_from_network(network: str) -> int:
    """Derive the numeric EVM chain id from an x402 network identifier."""
    if not isinstance(network, str) or not network.startswith("eip155:"):
        raise ValueError("x402 network must be an EVM CAIP-2 identifier")
    chain_id = int(network.removeprefix("eip155:"))
    if chain_id <= 0:
        raise ValueError("x402 chain id must be positive")
    return chain_id
