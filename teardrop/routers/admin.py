# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Admin-only routes: org/user provisioning, usage, billing, pricing, spending,
tool/memory/MCP/A2A inspection, and marketplace withdrawal operations.

All routes require the ``require_admin`` dependency. Extracted verbatim from
``teardrop.app`` with no logic changes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
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
from marketplace import (
    complete_withdrawal,
    get_marketplace_tool_by_name,
    list_pending_withdrawals,
    process_withdrawal,
)
from mcp_client import list_org_mcp_servers
from org_tools import list_org_tools
from teardrop.config import get_settings
from teardrop.dependencies import require_admin
from teardrop.memory import count_memories, delete_all_org_memories, list_memories
from teardrop.routers.org.mcp import _mcp_server_to_response
from teardrop.routers.org.tools import _org_tool_to_response
from teardrop.usage import get_usage_by_org, get_usage_by_user
from teardrop.users import create_client_credential, create_org, create_user
from tools import registry

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


class CreateOrgRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class CreateUserRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    secret: str = Field(..., min_length=8, max_length=128)
    org_id: str
    role: str = "user"


@router.post("/admin/orgs", tags=["Admin"])
async def admin_create_org(
    body: CreateOrgRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Create a new organisation (admin only)."""
    org = await create_org(body.name)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"id": org.id, "name": org.name},
    )


@router.post("/admin/users", tags=["Admin"])
async def admin_create_user(
    body: CreateUserRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Create a new user within an org (admin only)."""
    user = await create_user(
        email=body.email,
        secret=body.secret,
        org_id=body.org_id,
        role=body.role,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"id": user.id, "email": user.email, "org_id": user.org_id, "role": user.role},
    )


class CreateClientCredentialsRequest(BaseModel):
    org_id: str


@router.post("/admin/client-credentials", tags=["Admin"])
async def admin_create_client_credentials(
    body: CreateClientCredentialsRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Create org-scoped M2M client credentials (admin only).

    The client_secret is returned exactly once — store it immediately.
    """
    cred, plaintext_secret = await create_client_credential(body.org_id)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "client_id": cred.client_id,
            "client_secret": plaintext_secret,
            "org_id": cred.org_id,
            "created_at": cred.created_at.isoformat(),
        },
    )


# ─── Usage endpoints ─────────────────────────────────────────────────────────


@router.get("/admin/usage/{user_id}", tags=["Admin"])
async def admin_usage_user(
    user_id: str,
    _admin: dict = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
) -> JSONResponse:
    """Return aggregated usage for a specific user (admin only)."""

    start_dt = datetime.fromisoformat(start) if start else None
    end_dt = datetime.fromisoformat(end) if end else None
    summary = await get_usage_by_user(user_id, start_dt, end_dt)
    return JSONResponse(content=summary.model_dump())


@router.get("/admin/usage/org/{org_id}", tags=["Admin"])
async def admin_usage_org(
    org_id: str,
    _admin: dict = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
) -> JSONResponse:
    """Return aggregated usage for an entire org (admin only)."""

    start_dt = datetime.fromisoformat(start) if start else None
    end_dt = datetime.fromisoformat(end) if end else None
    summary = await get_usage_by_org(org_id, start_dt, end_dt)
    return JSONResponse(content=summary.model_dump())


# ─── Billing endpoints ───────────────────────────────────────────────────────


class ToolPricingOverrideRequest(BaseModel):
    tool_name: str = Field(..., min_length=1, max_length=100)
    cost_usdc: int = Field(..., ge=0, le=100_000_000)
    description: str = Field("", max_length=500)


@router.post("/admin/pricing/tools", tags=["Admin"])
async def admin_upsert_tool_pricing(
    body: ToolPricingOverrideRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Set or update the per-call cost for a specific tool (admin only).

    Accepts built-in tool names or qualified marketplace names (e.g. 'acme/weather').
    """
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


@router.delete("/admin/pricing/tools/{tool_name}", tags=["Admin"])
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


@router.get("/admin/billing/revenue", tags=["Admin"])
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


@router.post("/admin/credits/topup", tags=["Admin"])
async def admin_credits_topup(
    body: TopupRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Top up an org's prepaid credit balance (admin only)."""
    new_balance = await admin_topup_credit(body.org_id, body.amount_usdc)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"org_id": body.org_id, "new_balance_usdc": new_balance},
    )


@router.get("/admin/billing/pending", tags=["Admin"])
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


@router.post("/admin/billing/pending/{settlement_id}/retry", tags=["Admin"])
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


@router.get("/admin/orgs/{org_id}/spending", tags=["Admin"])
async def admin_get_spending(
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Get spending configuration for an org (admin only)."""
    config = await get_org_spending_config(org_id)
    return JSONResponse(content=config)


@router.patch("/admin/orgs/{org_id}/spending", tags=["Admin"])
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


@router.get("/admin/tools/{org_id}", tags=["Admin"])
async def admin_list_tools(
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: list all custom tools for a given org (including inactive)."""
    tools = await list_org_tools(org_id, active_only=False)
    return JSONResponse(content=[_org_tool_to_response(t) for t in tools])


@router.get("/admin/memories/org/{org_id}", tags=["Admin"])
async def admin_list_org_memories(
    org_id: str,
    _admin: dict = Depends(require_admin),
    limit: int = Query(default=50, ge=1, le=200),
) -> JSONResponse:
    """Admin: list memories for a specific org."""
    entries = await list_memories(org_id, limit)
    total = await count_memories(org_id)
    serialized = [
        {
            "id": e.id,
            "content": e.content,
            "user_id": e.user_id,
            "source_run_id": e.source_run_id,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]
    return JSONResponse(content={"items": serialized, "total": total})


@router.delete("/admin/memories/org/{org_id}", tags=["Admin"])
async def admin_purge_org_memories(
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: delete all memories for a specific org."""
    deleted_count = await delete_all_org_memories(org_id)
    return JSONResponse(content={"status": "purged", "deleted": deleted_count})


@router.get("/admin/mcp/servers/{org_id}", tags=["Admin"])
async def admin_list_mcp_servers(
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: list all MCP servers for an org."""
    servers = await list_org_mcp_servers(org_id, active_only=False)
    return JSONResponse(content=[_mcp_server_to_response(s) for s in servers])


# ─── A2A Delegation – Allowlist Admin ─────────────────────────────────────────


class CreateA2AAgentRequest(BaseModel):
    org_id: str
    agent_url: str = Field(..., min_length=10, max_length=2000)
    label: str | None = Field(default=None, max_length=200)
    max_cost_usdc: int = Field(default=0, description="Per-delegation cost cap in atomic USDC (0 = global default)")
    require_x402: bool = Field(default=False, description="Require x402 payment for this agent")
    jwt_forward: bool = Field(default=False, description="Forward caller JWT as Authorization header to this agent")


@router.post("/admin/a2a/agents", tags=["Admin"])
async def admin_add_a2a_agent(
    request: Request,
    body: CreateA2AAgentRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Add a trusted A2A agent to an org's allowlist."""
    pool: asyncpg.Pool = request.app.state.pool
    agent_id = str(uuid.uuid4())
    try:
        await pool.execute(
            """
            INSERT INTO a2a_allowed_agents (id, org_id, agent_url, label, max_cost_usdc, require_x402, jwt_forward)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            agent_id,
            body.org_id,
            body.agent_url.rstrip("/"),
            body.label,
            body.max_cost_usdc,
            body.require_x402,
            body.jwt_forward,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This agent URL is already in the org's allowlist",
        )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": agent_id,
            "org_id": body.org_id,
            "agent_url": body.agent_url,
            "label": body.label,
            "max_cost_usdc": body.max_cost_usdc,
            "require_x402": body.require_x402,
            "jwt_forward": body.jwt_forward,
        },
    )


@router.get("/admin/a2a/agents/{org_id}", tags=["Admin"])
async def admin_list_a2a_agents(
    request: Request,
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """List all trusted A2A agents for an org."""
    pool: asyncpg.Pool = request.app.state.pool
    rows = await pool.fetch(
        "SELECT id, org_id, agent_url, label, max_cost_usdc, require_x402, jwt_forward, created_at"
        " FROM a2a_allowed_agents WHERE org_id = $1 ORDER BY created_at",
        org_id,
    )
    return JSONResponse(
        content=[
            {
                "id": r["id"],
                "org_id": r["org_id"],
                "agent_url": r["agent_url"],
                "label": r["label"],
                "max_cost_usdc": r["max_cost_usdc"],
                "require_x402": r["require_x402"],
                "jwt_forward": r["jwt_forward"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    )


@router.delete("/admin/a2a/agents/{agent_id}", tags=["Admin"])
async def admin_delete_a2a_agent(
    request: Request,
    agent_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Remove an A2A agent from an org's allowlist."""
    pool: asyncpg.Pool = request.app.state.pool
    result = await pool.execute(
        "DELETE FROM a2a_allowed_agents WHERE id = $1",
        agent_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return JSONResponse(content={"deleted": agent_id})


# ─── MCP Marketplace – Admin Withdrawal Operations ───────────────────────────


@router.post("/admin/marketplace/process-withdrawal/{withdrawal_id}", tags=["Admin"])
async def admin_process_withdrawal(
    withdrawal_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: process a pending withdrawal (mark earnings as settled)."""
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


@router.post("/admin/marketplace/complete-withdrawal/{withdrawal_id}", tags=["Admin"])
async def admin_complete_withdrawal(
    withdrawal_id: str,
    body: CompleteWithdrawalRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: record the on-chain tx_hash for a processed withdrawal."""
    try:
        await complete_withdrawal(withdrawal_id, body.tx_hash)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return JSONResponse(content={"status": "completed", "tx_hash": body.tx_hash})


@router.get("/admin/marketplace/withdrawals", tags=["Admin"])
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


@router.post("/admin/marketplace/sweep", tags=["Admin"])
async def admin_marketplace_sweep(
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: manually trigger a marketplace withdrawal sweep."""
    from marketplace import marketplace_sweep_once

    count = await marketplace_sweep_once()
    return JSONResponse(content={"processed": count})


@router.get("/admin/marketplace/sweep-status", tags=["Admin"])
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


@router.post("/admin/marketplace/sweep-retry/{withdrawal_id}", tags=["Admin"])
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


@router.post("/admin/marketplace/reset-withdrawal/{withdrawal_id}", tags=["Admin"])
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


@router.get("/admin/marketplace/settlement-balance", tags=["Admin"])
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
