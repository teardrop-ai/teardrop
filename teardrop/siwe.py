# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Sign-In With Ethereum (SIWE) verification and login helpers.

Extracted verbatim from ``teardrop.app``. ``_verify_siwe`` is shared by the
auth router (``/token``) and the wallets router (``/wallets/link``);
``_handle_siwe_login`` is auth-specific but co-located here because it is
tightly coupled to ``_verify_siwe``.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException

from teardrop.auth import create_access_token
from teardrop.config import get_settings
from teardrop.users import (
    create_org,
    create_refresh_token,
    create_user,
    get_org_by_name,
    get_user_by_org_id,
)
from teardrop.wallets import consume_nonce, create_wallet, get_wallet_by_address

logger = logging.getLogger(__name__)
settings = get_settings()

__all__ = ["_verify_siwe", "_handle_siwe_login"]


async def _verify_siwe(siwe_message: str, siwe_signature: str) -> tuple[str, int]:
    """Parse and verify a SIWE message, consume its nonce, and return (address, chain_id).

    Raises HTTPException on any failure.  Signature is verified BEFORE the
    nonce is consumed to prevent nonce-exhaustion DoS attacks.
    """
    import siwe as siwe_errors
    from siwe import SiweMessage

    try:
        msg = SiweMessage.from_message(siwe_message)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed SIWE message")

    expected_domain = settings.effective_siwe_domain
    if msg.domain != expected_domain:
        raise HTTPException(status_code=400, detail=f"Domain mismatch: expected {expected_domain}")

    # Verify EIP-191 signature BEFORE consuming nonce.
    # This prevents nonce-exhaustion DoS: an invalid signature must never burn
    # a legitimate nonce. The nonce is embedded in the signed SIWE message, so
    # an attacker cannot forge a valid signature for someone else's nonce.
    try:
        msg.verify(signature=siwe_signature)
    except (
        siwe_errors.ExpiredMessage,
        siwe_errors.InvalidSignature,
        siwe_errors.DomainMismatch,
        siwe_errors.NonceMismatch,
        siwe_errors.MalformedSession,
    ):
        raise HTTPException(status_code=401, detail="SIWE signature verification failed")
    except Exception:
        raise HTTPException(status_code=401, detail="SIWE verification error")

    from web3 import Web3

    address = Web3.to_checksum_address(msg.address)
    chain_id = int(msg.chain_id) if msg.chain_id else 1

    # Consume nonce AFTER signature verification (single-use + TTL + address binding)
    if not await consume_nonce(msg.nonce, settings.siwe_nonce_ttl_seconds, expected_address=address):
        raise HTTPException(status_code=401, detail="Invalid or expired nonce")
    logger.info("SIWE nonce consumed for address=%s chain=%d", address, chain_id)

    return address, chain_id


async def _handle_siwe_login(siwe_message: str, siwe_signature: str) -> dict:
    """Verify a SIWE message, auto-register if needed, and return a JWT."""
    address, chain_id = await _verify_siwe(siwe_message, siwe_signature)

    # Look up existing wallet
    wallet = await get_wallet_by_address(address, chain_id)

    if wallet is None:
        # Org may already exist from a previous partial registration
        org_name = f"wallet-{address[:10].lower()}"
        existing_org = await get_org_by_name(org_name)
        if existing_org:
            org = existing_org
            existing_user = await get_user_by_org_id(org.id)
            user = existing_user or await create_user(
                email=f"{address.lower()}@wallet",
                secret=secrets.token_urlsafe(32),
                org_id=org.id,
                role="user",
            )
        else:
            org = await create_org(org_name)
            user = await create_user(
                email=f"{address.lower()}@wallet",
                secret=secrets.token_urlsafe(32),
                org_id=org.id,
                role="user",
            )
        wallet = await create_wallet(
            address=address,
            chain_id=chain_id,
            user_id=user.id,
            org_id=org.id,
            is_primary=True,
        )
        logger.info("SIWE auto-registered user=%s address=%s", user.id, address)

    siwe_claims = {
        "org_id": wallet.org_id,
        "address": address,
        "chain_id": chain_id,
        "auth_method": "siwe",
        "role": "user",
        "email": f"{address.lower()}@wallet",
    }
    access_token = create_access_token(subject=wallet.user_id, extra_claims=siwe_claims)
    siwe_refresh = await create_refresh_token(
        user_id=wallet.user_id,
        org_id=wallet.org_id,
        auth_method="siwe",
        extra_claims=siwe_claims,
        expire_days=settings.refresh_token_expire_days,
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.jwt_access_token_expire_minutes * 60,
        "refresh_token": siwe_refresh,
    }
