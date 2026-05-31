# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Admin billing operations: tool pricing overrides, revenue, credit top-ups,
settlement reconciliation, and per-org spending configuration.

All routes require the ``require_admin`` dependency. Extracted verbatim from
``teardrop.routers.admin`` with no logic changes.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from billing import (
    admin_topup_credit,
    delete_tool_pricing_override,
    get_org_spending_config,
    get_pending_settlements,
    get_revenue_summary,
    reset_exhausted_settlement,
    update_org_spending_config,
    upsert_tool_pricing_override,
)
from marketplace import get_marketplace_tool_by_name
from teardrop.config import get_settings
from teardrop.dependencies import require_admin
from teardrop.rate_limit import _enforce_rate_limit
from tools import registry

settings = get_settings()

router = APIRouter()


class ToolPricingOverrideRequest(BaseModel):
    tool_name: str = Field(..., min_length=1, max_length=100)
    cost_usdc: int = Field(..., ge=0, le=100_000_000)
    description: str = Field("", max_length=500)


@router.post("/admin/pricing/tools", tags=["Admin", "Admin / Billing"])
async def admin_upsert_tool_pricing(
    body: ToolPricingOverrideRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Set or update the per-call cost for a specific tool (admin only).

    Accepts built-in tool names or qualified marketplace names (e.g. 'acme/weather').
    """
    await _enforce_rate_limit(
        f"admin:{_admin.get('sub', 'unknown')}",
        settings.rate_limit_topup_rpm,
        detail="Rate limit exceeded for admin operations.",
    )
    known_names = {t.name for t in registry.list_latest(include_deprecated=True)}
    tool_valid = body.tool_name in known_names

    # Also accept qualified marketplace tool names
    if not tool_valid and "/" in body.tool_name:
        slug, tname = body.tool_name.split("/", 1)
        mp_tool = await get_marketplace_tool_by_name(tname, slug)
        tool_valid = mp_tool is not None

    if not tool_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown tool name: {body.tool_name!r}. Must be a registered tool or qualified marketplace name.",
        )
    await upsert_tool_pricing_override(body.tool_name, body.cost_usdc, body.description)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "tool_name": body.tool_name,
            "cost_usdc": body.cost_usdc,
            "description": body.description,
            "updated": True,
        },
    )


@router.delete("/admin/pricing/tools/{tool_name}", tags=["Admin", "Admin / Billing"])
async def admin_delete_tool_pricing(
    tool_name: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Remove a per-tool pricing override, reverting to the global default (admin only)."""
    deleted = await delete_tool_pricing_override(tool_name)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pricing override found for tool: {tool_name!r}",
        )
    return JSONResponse(content={"deleted": True, "tool_name": tool_name})


@router.get("/admin/billing/revenue", tags=["Admin", "Admin / Billing"])
async def admin_billing_revenue(
    _admin: dict = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
) -> JSONResponse:
    """Aggregate settled revenue by period (admin only)."""

    start_dt = datetime.fromisoformat(start) if start else None
    end_dt = datetime.fromisoformat(end) if end else None
    summary = await get_revenue_summary(start_dt, end_dt)
    return JSONResponse(content=summary)


class TopupRequest(BaseModel):
    org_id: str
    amount_usdc: int = Field(..., gt=0)


@router.post("/admin/credits/topup", tags=["Admin", "Admin / Billing"])
async def admin_credits_topup(
    body: TopupRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Top up an org's prepaid credit balance (admin only)."""
    await _enforce_rate_limit(
        f"admin:{_admin.get('sub', 'unknown')}",
        settings.rate_limit_topup_rpm,
        detail="Rate limit exceeded for admin operations.",
    )
    # Record the acting admin in the immutable credit ledger ``reason`` so the
    # financial audit trail attributes the top-up to a specific operator.
    reason = f"admin_topup by {_admin.get('sub', 'unknown')}"
    new_balance = await admin_topup_credit(body.org_id, body.amount_usdc, reason=reason)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"org_id": body.org_id, "new_balance_usdc": new_balance},
    )


@router.get("/admin/billing/pending", tags=["Admin", "Admin / Billing"])
async def admin_billing_pending(
    _admin: dict = Depends(require_admin),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = 50,
) -> JSONResponse:
    """List pending/failed settlements for reconciliation (admin only)."""
    rows = await get_pending_settlements(status_filter, min(limit, 200))
    serialized = []
    for r in rows:
        row = dict(r)
        for key in ("next_retry_at", "created_at"):
            if key in row and row[key] is not None:
                row[key] = row[key].isoformat()
        serialized.append(row)
    return JSONResponse(content={"items": serialized})


@router.post("/admin/billing/pending/{settlement_id}/retry", tags=["Admin", "Admin / Billing"])
async def admin_billing_retry(
    settlement_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Reset an exhausted settlement for manual retry (admin only)."""
    ok = await reset_exhausted_settlement(settlement_id)
    if ok is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "x402 settlements cannot be retried: payment payloads are single-use. "
                "Reconcile manually via /admin/billing/pending."
            ),
        )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Settlement not found or not in 'exhausted' status",
        )
    return JSONResponse(content={"settlement_id": settlement_id, "status": "pending"})


class SpendingConfigUpdate(BaseModel):
    spending_limit_usdc: int | None = None
    is_paused: bool | None = None


@router.get("/admin/orgs/{org_id}/spending", tags=["Admin", "Admin / Billing"])
async def admin_get_spending(
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Get spending configuration for an org (admin only)."""
    config = await get_org_spending_config(org_id)
    return JSONResponse(content=config)


@router.patch("/admin/orgs/{org_id}/spending", tags=["Admin", "Admin / Billing"])
async def admin_update_spending(
    org_id: str,
    body: SpendingConfigUpdate,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Update spending limit or pause/unpause an org (admin only)."""
    result = await update_org_spending_config(
        org_id,
        spending_limit_usdc=body.spending_limit_usdc,
        is_paused=body.is_paused,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Org not found in credit system",
        )
    return JSONResponse(content=result)
