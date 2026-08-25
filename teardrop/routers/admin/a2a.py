# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Admin A2A delegation allowlist management.

All routes require the ``require_admin`` dependency. Extracted verbatim from
``teardrop.routers.admin`` with no logic changes.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shared.db_pool import PgPool, UniqueViolation
from teardrop.config import get_settings
from teardrop.dependencies import require_admin
from teardrop.rate_limit import _enforce_rate_limit

router = APIRouter()
settings = get_settings()


class CreateA2AAgentRequest(BaseModel):
    org_id: str
    agent_url: str = Field(..., min_length=10, max_length=2000)
    label: str | None = Field(default=None, max_length=200)
    max_cost_usdc: int = Field(default=0, description="Per-delegation cost cap in atomic USDC (0 = global default)")
    require_x402: bool = Field(default=False, description="Require x402 payment for this agent")
    jwt_forward: bool = Field(default=False, description="Forward caller JWT as Authorization header to this agent")


class A2AAgentResponse(BaseModel):
    id: str
    org_id: str
    agent_url: str
    label: str | None = None
    max_cost_usdc: int
    require_x402: bool
    jwt_forward: bool


class A2AAgentListItem(A2AAgentResponse):
    created_at: str | None = Field(default=None, description="ISO 8601 timestamp; null if unavailable.")


@router.post(
    "/admin/a2a/agents", tags=["Admin", "Admin / A2A"], response_model=A2AAgentResponse, status_code=status.HTTP_201_CREATED
)
async def admin_add_a2a_agent(
    request: Request,
    body: CreateA2AAgentRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Add a trusted A2A agent to an org's allowlist."""
    pool: PgPool = request.app.state.pool
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
    except UniqueViolation:
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


@router.get("/admin/a2a/agents/{org_id}", tags=["Admin", "Admin / A2A"], response_model=list[A2AAgentListItem])
async def admin_list_a2a_agents(
    request: Request,
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """List all trusted A2A agents for an org."""
    pool: PgPool = request.app.state.pool
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


class A2AAgentDeletedResponse(BaseModel):
    deleted: str = Field(..., description="The deleted agent's id.")


@router.delete("/admin/a2a/agents/{agent_id}", tags=["Admin", "Admin / A2A"], response_model=A2AAgentDeletedResponse)
async def admin_delete_a2a_agent(
    request: Request,
    agent_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Remove an A2A agent from an org's allowlist."""
    pool: PgPool = request.app.state.pool
    result = await pool.execute(
        "DELETE FROM a2a_allowed_agents WHERE id = $1",
        agent_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return JSONResponse(content={"deleted": agent_id})


class PossiblyDeliveredDelegationItem(BaseModel):
    id: str
    org_id: str
    run_id: str
    amount_usdc: int
    refund_status: str
    delivery_status: Literal["possibly_delivered"]
    agent_url: str | None = None
    agent_name: str | None = None
    task_status: str | None = None
    task_type: str | None = None
    billing_method: str | None = None
    settlement_tx: str | None = None
    delivery_settlement_tx: str | None = None
    delivery_error: str | None = None
    delivery_started_at: str | None = Field(default=None, description="ISO 8601 timestamp.")
    created_at: str | None = Field(default=None, description="ISO 8601 timestamp.")


@router.get(
    "/admin/a2a/delegations/possibly-delivered",
    tags=["Admin", "Admin / A2A"],
    response_model=list[PossiblyDeliveredDelegationItem],
)
async def admin_list_possibly_delivered_delegations(
    org_id: str | None = Query(default=None),
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """List paid delegations whose delivery outcome needs operator review."""
    from billing import get_possibly_delivered_delegations

    rows = await get_possibly_delivered_delegations(org_id, limit=200)

    def _iso(value) -> str | None:
        return value.isoformat() if value else None

    return JSONResponse(
        content=[
            {
                "id": row["id"],
                "org_id": row["org_id"],
                "run_id": row["run_id"],
                "amount_usdc": row["amount_usdc"],
                "refund_status": row["refund_status"],
                "delivery_status": row["delivery_status"],
                "agent_url": row.get("agent_url"),
                "agent_name": row.get("agent_name"),
                "task_status": row.get("task_status"),
                "task_type": row.get("task_type"),
                "billing_method": row.get("billing_method"),
                "settlement_tx": row.get("settlement_tx"),
                "delivery_settlement_tx": row.get("delivery_settlement_tx") or None,
                "delivery_error": row.get("delivery_error") or None,
                "delivery_started_at": _iso(row.get("delivery_started_at")),
                "created_at": _iso(row.get("created_at")),
            }
            for row in rows
        ]
    )


class ResolveA2ADelegationRequest(BaseModel):
    org_id: str = Field(..., min_length=1, max_length=200)
    outcome: Literal["confirmed", "failed"]
    settlement_tx: str | None = Field(default=None, max_length=66, pattern=r"^0x[a-fA-F0-9]{64}$")
    reason: str = Field(default="", max_length=500)


class ResolveA2ADelegationResponse(BaseModel):
    id: str
    org_id: str
    outcome: Literal["confirmed", "failed"]
    refund_status: Literal["cancelled", "refunded"]


@router.post(
    "/admin/a2a/delegations/{delegation_id}/resolve",
    tags=["Admin", "Admin / A2A"],
    response_model=ResolveA2ADelegationResponse,
)
async def admin_resolve_a2a_delegation(
    delegation_id: str,
    body: ResolveA2ADelegationRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Resolve one ambiguous delivery without re-dispatching the task."""
    await _enforce_rate_limit(
        f"admin:{_admin.get('sub', 'unknown')}",
        settings.rate_limit_topup_rpm,
        detail="Rate limit exceeded for admin operations.",
    )
    from billing import confirm_delegation_delivery, fail_delegation_delivery

    if body.outcome == "confirmed":
        resolved = await confirm_delegation_delivery(body.org_id, delegation_id, body.settlement_tx or "")
        refund_status = "cancelled"
    else:
        resolved = await fail_delegation_delivery(body.org_id, delegation_id, body.reason)
        refund_status = "refunded"

    if not resolved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Delegation not found, already resolved, or incompatible with this outcome.",
        )
    return JSONResponse(
        content={
            "id": delegation_id,
            "org_id": body.org_id,
            "outcome": body.outcome,
            "refund_status": refund_status,
        }
    )
