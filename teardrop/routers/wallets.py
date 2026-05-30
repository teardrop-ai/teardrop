# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""User wallet linking and CDP-backed agent wallet routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from teardrop.agent_wallets import (
    create_agent_wallet,
    deactivate_agent_wallet,
    get_agent_wallet,
    get_agent_wallet_balance,
)
from teardrop.config import get_settings
from teardrop.dependencies import require_admin, require_auth
from teardrop.siwe import _verify_siwe
from teardrop.wallets import (
    create_wallet,
    delete_wallet,
    get_wallet_by_address,
    get_wallets_by_user,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── Wallet endpoints ─────────────────────────────────────────────────────────


class LinkWalletRequest(BaseModel):
    siwe_message: str
    siwe_signature: str


@router.post("/wallets/link", tags=["Wallets"])
async def link_wallet(
    body: LinkWalletRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Link an additional wallet to the authenticated user via SIWE."""
    address, chain_id = await _verify_siwe(body.siwe_message, body.siwe_signature)

    existing = await get_wallet_by_address(address, chain_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Wallet already linked")

    wallet = await create_wallet(
        address=address,
        chain_id=chain_id,
        user_id=payload["sub"],
        org_id=payload.get("org_id", ""),
        is_primary=False,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"id": wallet.id, "address": wallet.address, "chain_id": wallet.chain_id},
    )


@router.get("/wallets/me", tags=["Wallets"])
async def list_wallets(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List all wallets linked to the authenticated user."""
    wallets = await get_wallets_by_user(payload["sub"])
    return JSONResponse(
        content=[
            {
                "id": w.id,
                "address": w.address,
                "chain_id": w.chain_id,
                "is_primary": w.is_primary,
                "created_at": w.created_at.isoformat(),
            }
            for w in wallets
        ]
    )


# ─── Agent Wallet endpoints ──────────────────────────────────────────────────


@router.post("/wallets/agent", tags=["Agent Wallets"])
async def provision_agent_wallet(
    payload: dict = Depends(require_auth),
    chain_id: int | None = None,
) -> JSONResponse:
    """Provision a CDP-backed agent wallet for the caller's org."""
    settings = get_settings()
    if not settings.agent_wallet_enabled:
        raise HTTPException(status_code=501, detail="Agent wallets are not enabled")
    try:
        wallet = await create_agent_wallet(
            org_id=payload.get("org_id", ""),
            actor_id=payload["sub"],
            chain_id=chain_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": wallet.id,
            "address": wallet.address,
            "chain_id": wallet.chain_id,
            "wallet_type": wallet.wallet_type,
            "is_active": wallet.is_active,
            "created_at": wallet.created_at.isoformat(),
        },
    )


@router.get("/wallets/agent", tags=["Agent Wallets"])
async def get_agent_wallet_info(
    payload: dict = Depends(require_auth),
    chain_id: int | None = None,
    include_balance: bool = False,
) -> JSONResponse:
    """Return the org's agent wallet, optionally including on-chain USDC balance."""
    settings = get_settings()
    if not settings.agent_wallet_enabled:
        raise HTTPException(status_code=501, detail="Agent wallets are not enabled")
    wallet = await get_agent_wallet(
        org_id=payload.get("org_id", ""),
        chain_id=chain_id,
    )
    if wallet is None:
        raise HTTPException(status_code=404, detail="No agent wallet found for this org")
    result: dict = {
        "id": wallet.id,
        "address": wallet.address,
        "chain_id": wallet.chain_id,
        "wallet_type": wallet.wallet_type,
        "is_active": wallet.is_active,
        "created_at": wallet.created_at.isoformat(),
    }
    if include_balance:
        try:
            balance_info = await get_agent_wallet_balance(
                org_id=payload.get("org_id", ""),
                chain_id=chain_id,
            )
            result["balance_usdc"] = balance_info["balance_usdc"]
        except Exception:
            result["balance_usdc"] = None
            result["balance_error"] = "Failed to fetch on-chain balance"
    return JSONResponse(content=result)


@router.delete("/wallets/agent", tags=["Agent Wallets"])
async def deactivate_org_agent_wallet(
    _admin: dict = Depends(require_admin),
    chain_id: int | None = None,
) -> JSONResponse:
    """Deactivate the org's agent wallet (admin only)."""
    settings = get_settings()
    if not settings.agent_wallet_enabled:
        raise HTTPException(status_code=501, detail="Agent wallets are not enabled")
    deactivated = await deactivate_agent_wallet(
        org_id=_admin.get("org_id", ""),
        actor_id=_admin["sub"],
        chain_id=chain_id,
    )
    if not deactivated:
        raise HTTPException(status_code=404, detail="No active agent wallet found")
    return JSONResponse(content={"status": "deactivated"})


@router.delete("/wallets/{wallet_id}", tags=["Wallets"])
async def unlink_wallet(
    wallet_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Unlink a wallet from the authenticated user."""
    deleted = await delete_wallet(wallet_id, payload["sub"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Wallet not found or not owned by you")
    return JSONResponse(content={"status": "deleted"})
