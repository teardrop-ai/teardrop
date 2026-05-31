# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Admin MCP marketplace withdrawal & settlement operations.

All routes require the ``require_admin`` dependency. Extracted verbatim from
``teardrop.routers.admin`` with no logic changes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from marketplace import (
    complete_withdrawal,
    list_pending_withdrawals,
    process_withdrawal,
)
from teardrop.config import get_settings
from teardrop.dependencies import require_admin
from teardrop.rate_limit import _enforce_rate_limit

settings = get_settings()

router = APIRouter()


@router.post("/admin/marketplace/process-withdrawal/{withdrawal_id}", tags=["Admin", "Admin / Marketplace"])
async def admin_process_withdrawal(
    withdrawal_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: process a pending withdrawal (mark earnings as settled)."""
    await _enforce_rate_limit(
        f"admin:{_admin.get('sub', 'unknown')}",
        settings.rate_limit_topup_rpm,
        detail="Rate limit exceeded for admin operations.",
    )
    try:
        withdrawal = await process_withdrawal(withdrawal_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))

    return JSONResponse(
        content={
            "id": withdrawal.id,
            "org_id": withdrawal.org_id,
            "amount_usdc": withdrawal.amount_usdc,
            "status": withdrawal.status,
        }
    )


class CompleteWithdrawalRequest(BaseModel):
    tx_hash: str = Field(..., min_length=10, max_length=100)


@router.post("/admin/marketplace/complete-withdrawal/{withdrawal_id}", tags=["Admin", "Admin / Marketplace"])
async def admin_complete_withdrawal(
    withdrawal_id: str,
    body: CompleteWithdrawalRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: record the on-chain tx_hash for a processed withdrawal."""
    await _enforce_rate_limit(
        f"admin:{_admin.get('sub', 'unknown')}",
        settings.rate_limit_topup_rpm,
        detail="Rate limit exceeded for admin operations.",
    )
    try:
        await complete_withdrawal(withdrawal_id, body.tx_hash)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return JSONResponse(content={"status": "completed", "tx_hash": body.tx_hash})


@router.get("/admin/marketplace/withdrawals", tags=["Admin", "Admin / Marketplace"])
async def admin_list_withdrawals(
    org_id: str | None = Query(default=None),
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: list pending withdrawals, optionally filtered by org."""
    withdrawals = await list_pending_withdrawals(org_id)
    return JSONResponse(
        content={
            "withdrawals": [
                {
                    "id": w.id,
                    "org_id": w.org_id,
                    "amount_usdc": w.amount_usdc,
                    "wallet": w.wallet,
                    "status": w.status,
                    "created_at": w.created_at.isoformat(),
                    "settled_at": w.settled_at.isoformat() if w.settled_at else None,
                }
                for w in withdrawals
            ]
        }
    )


@router.post("/admin/marketplace/sweep", tags=["Admin", "Admin / Marketplace"])
async def admin_marketplace_sweep(
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: manually trigger a marketplace withdrawal sweep."""
    await _enforce_rate_limit(
        f"admin:{_admin.get('sub', 'unknown')}",
        settings.rate_limit_topup_rpm,
        detail="Rate limit exceeded for admin operations.",
    )
    from marketplace import marketplace_sweep_once

    count = await marketplace_sweep_once()
    return JSONResponse(content={"processed": count})


@router.get("/admin/marketplace/sweep-status", tags=["Admin", "Admin / Marketplace"])
async def admin_marketplace_sweep_status(
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: return pending/failed/exhausted withdrawal state for operator review.

    Useful for diagnosing why an org's earnings have not been settled.
    """
    from marketplace import list_exhausted_withdrawals, list_pending_withdrawals

    pending = await list_pending_withdrawals()
    exhausted = await list_exhausted_withdrawals()

    def _fmt(w: object) -> dict:
        from marketplace import AuthorWithdrawal  # noqa: PLC0415

        assert isinstance(w, AuthorWithdrawal)
        return {
            "id": w.id,
            "org_id": w.org_id,
            "amount_usdc": w.amount_usdc,
            "status": w.status,
            "sweep_attempt_count": w.sweep_attempt_count,
            "last_sweep_error": w.last_sweep_error,
            "next_sweep_at": w.next_sweep_at.isoformat() if w.next_sweep_at else None,
            "created_at": w.created_at.isoformat(),
        }

    return JSONResponse(
        content={
            "pending": [_fmt(w) for w in pending],
            "exhausted": [_fmt(w) for w in exhausted],
        }
    )


@router.post("/admin/marketplace/sweep-retry/{withdrawal_id}", tags=["Admin", "Admin / Marketplace"])
async def admin_marketplace_sweep_retry(
    withdrawal_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: reset a failed or exhausted withdrawal so the next sweep retries it."""
    from marketplace import reset_withdrawal

    found = await reset_withdrawal(withdrawal_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Withdrawal not found or not in 'failed'/'exhausted' status.",
        )
    return JSONResponse(content={"status": "pending", "id": withdrawal_id})


@router.post("/admin/marketplace/reset-withdrawal/{withdrawal_id}", tags=["Admin", "Admin / Marketplace"])
async def admin_reset_withdrawal(
    withdrawal_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: reset a failed withdrawal to pending so it can be re-processed."""
    from marketplace import reset_withdrawal

    found = await reset_withdrawal(withdrawal_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Withdrawal not found or not in 'failed' status.",
        )
    return JSONResponse(content={"status": "pending", "id": withdrawal_id})


@router.get("/admin/marketplace/settlement-balance", tags=["Admin", "Admin / Marketplace"])
async def admin_marketplace_settlement_balance(
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: query the USDC balance of the marketplace settlement CDP wallet."""
    from teardrop.agent_wallets import _chain_id_to_network, _get_cdp_client, _require_cdp_enabled

    _require_cdp_enabled()
    chain_id = settings.marketplace_settlement_chain_id
    network = _chain_id_to_network(chain_id)
    account_name = settings.marketplace_settlement_cdp_account

    balance_usdc = 0
    async with _get_cdp_client() as cdp:
        account = await cdp.evm.get_or_create_account(name=account_name)
        balances = await cdp.evm.list_token_balances(address=account.address, network=network)
        for tb in balances:
            symbol = getattr(tb, "symbol", "") or ""
            if symbol.upper() == "USDC":
                from decimal import Decimal

                amt = getattr(tb, "amount", None)
                if amt is not None:
                    balance_usdc = int(Decimal(str(amt)) * Decimal("1_000_000"))
                break

    return JSONResponse(
        content={
            "account": account_name,
            "address": account.address,
            "chain_id": chain_id,
            "balance_usdc": balance_usdc,
        }
    )
