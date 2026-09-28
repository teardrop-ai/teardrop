# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Org-scoped A2A agent allowlist management and delegation history routes."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shared.db_pool import PgPool, UniqueViolation
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.rate_limit import _enforce_rate_limit

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


# ─── A2A Delegation – Org-scoped Agent Management ────────────────────────────


class OrgCreateA2AAgentRequest(BaseModel):
    agent_url: str = Field(..., min_length=10, max_length=2000)
    label: str | None = Field(default=None, max_length=200)
    max_cost_usdc: int = Field(
        default=0,
        ge=0,
        description="Per-delegation cost cap in atomic USDC (0 = global default, or the self-serve maximum for members)",
    )
    require_x402: bool = Field(default=False, description="Require x402 payment for this agent (always true for members)")
    jwt_forward: bool = Field(default=False, description="Forward caller JWT as Authorization header (org admins only)")


class OrgA2AAgentResponse(BaseModel):
    id: str
    org_id: str
    agent_url: str
    label: str | None = None
    max_cost_usdc: int
    require_x402: bool
    jwt_forward: bool
    source: str = Field(default="admin", description="admin or self_serve")


class OrgA2AAgentListItem(BaseModel):
    id: str
    agent_url: str
    label: str | None = None
    max_cost_usdc: int
    require_x402: bool
    jwt_forward: bool
    source: str = Field(default="admin", description="admin or self_serve")
    created_at: str | None = Field(default=None, description="ISO 8601 timestamp; null if unavailable.")


def _max_self_serve_cost_usdc() -> int:
    """Largest per-row cap whose fee-inclusive pre-debit still passes the global delegation cap."""
    global_cap = settings.a2a_delegation_max_cost_usdc
    fee_bps = settings.a2a_delegation_platform_fee_bps
    cap = global_cap * 10_000 // (10_000 + fee_bps)
    while cap + 1 + ((cap + 1) * fee_bps) // 10_000 <= global_cap:
        cap += 1
    return cap


async def _resolve_self_serve_request(org_id: str, body: OrgCreateA2AAgentRequest, pool: PgPool) -> tuple[str, int]:
    """Validate a member's allowlist request; return the canonical URL and effective cap."""
    from billing import is_promotional_credit
    from teardrop.a2a_client import _canonicalize_agent_url

    if body.jwt_forward:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forwarding the caller JWT requires an org admin.",
        )
    try:
        agent_url = _canonicalize_agent_url(body.agent_url, require_https=True)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None

    max_allowed = _max_self_serve_cost_usdc()
    max_cost_usdc = body.max_cost_usdc or max_allowed
    if not 0 < max_cost_usdc <= max_allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"max_cost_usdc must be between 1 and {max_allowed} atomic USDC for member-added agents.",
        )

    listing_org_id = await pool.fetchval("SELECT org_id FROM a2a_agent_registry WHERE agent_url = $1", agent_url)
    if listing_org_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only agents listed in the marketplace registry can be added without an org admin.",
        )
    if listing_org_id == org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An organization cannot allowlist its own registered agent.",
        )
    if await is_promotional_credit(org_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Promotional credit cannot fund agent delegation. Top up credits first.",
        )
    return agent_url, max_cost_usdc


async def _insert_allowlist_event(
    conn,
    *,
    org_id: str,
    agent_id: str,
    agent_url: str,
    event_type: str,
    source: str,
    max_cost_usdc: int,
    actor_id: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO a2a_allowed_agent_events
            (id, org_id, allowed_agent_id, agent_url, event_type, source, max_cost_usdc, actor_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        str(uuid.uuid4()),
        org_id,
        agent_id,
        agent_url,
        event_type,
        source,
        max_cost_usdc,
        actor_id,
    )


@router.post("/a2a/agents", tags=["A2A"], response_model=OrgA2AAgentResponse, status_code=status.HTTP_201_CREATED)
async def add_a2a_agent(
    request: Request,
    body: OrgCreateA2AAgentRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Add a trusted A2A agent to the authenticated org's allowlist.

    Org admins may add any URL with any settings. Other members may add only an
    agent listed in the marketplace registry; those rows are forced to x402-only
    payment, no JWT forwarding, and a cap within the global delegation limit.
    """
    org_id = _require_org_id(payload)
    is_admin = payload.get("role") == "admin"

    # Per-org rate limit — registering an allowlist entry exposes a URL to the
    # agent's delegate_to_agent tool, so cap the write rate to defend against a
    # stolen admin JWT bulk-injecting malicious endpoints.
    await _enforce_rate_limit(
        f"a2a_add:{org_id}",
        settings.rate_limit_auth_rpm,
        detail="Rate limit exceeded for A2A agent registration.",
    )

    pool: PgPool = request.app.state.pool
    if is_admin:
        source = "admin"
        agent_url = body.agent_url.rstrip("/")
        max_cost_usdc = body.max_cost_usdc
        require_x402 = body.require_x402
        jwt_forward = body.jwt_forward
    else:
        source = "self_serve"
        agent_url, max_cost_usdc = await _resolve_self_serve_request(org_id, body, pool)
        require_x402 = True
        jwt_forward = False

    agent_id = str(uuid.uuid4())
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO a2a_allowed_agents
                        (id, org_id, agent_url, label, max_cost_usdc, require_x402, jwt_forward, source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    agent_id,
                    org_id,
                    agent_url,
                    body.label,
                    max_cost_usdc,
                    require_x402,
                    jwt_forward,
                    source,
                )
                await _insert_allowlist_event(
                    conn,
                    org_id=org_id,
                    agent_id=agent_id,
                    agent_url=agent_url,
                    event_type="created",
                    source=source,
                    max_cost_usdc=max_cost_usdc,
                    actor_id=str(payload.get("sub", "")),
                )
    except UniqueViolation:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This agent URL is already in your allowlist",
        )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": agent_id,
            "org_id": org_id,
            "agent_url": agent_url,
            "label": body.label,
            "max_cost_usdc": max_cost_usdc,
            "require_x402": require_x402,
            "jwt_forward": jwt_forward,
            "source": source,
        },
    )


@router.get("/a2a/agents", tags=["A2A"], response_model=list[OrgA2AAgentListItem])
async def list_a2a_agents(
    request: Request,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List all trusted A2A agents for the authenticated org."""
    org_id: str = payload.get("org_id", payload["sub"])
    pool: PgPool = request.app.state.pool
    rows = await pool.fetch(
        "SELECT id, org_id, agent_url, label, max_cost_usdc, require_x402, jwt_forward, source, created_at"
        " FROM a2a_allowed_agents WHERE org_id = $1 ORDER BY created_at",
        org_id,
    )
    return JSONResponse(
        content=[
            {
                "id": r["id"],
                "agent_url": r["agent_url"],
                "label": r["label"],
                "max_cost_usdc": r["max_cost_usdc"],
                "require_x402": r["require_x402"],
                "jwt_forward": r["jwt_forward"],
                "source": r["source"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    )


class OrgA2AAgentDeletedResponse(BaseModel):
    deleted: str = Field(..., description="The deleted agent's id.")


@router.delete("/a2a/agents/{agent_id}", tags=["A2A"], response_model=OrgA2AAgentDeletedResponse)
async def delete_a2a_agent(
    request: Request,
    agent_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Remove an A2A agent from the org's allowlist (members may remove only self-serve entries)."""
    org_id = _require_org_id(payload)
    is_admin = payload.get("role") == "admin"
    pool: PgPool = request.app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                DELETE FROM a2a_allowed_agents
                WHERE id = $1 AND org_id = $2 AND ($3 OR source = 'self_serve')
                RETURNING agent_url, source, max_cost_usdc
                """,
                agent_id,
                org_id,
                is_admin,
            )
            if row is None:
                exists = await conn.fetchval(
                    "SELECT 1 FROM a2a_allowed_agents WHERE id = $1 AND org_id = $2",
                    agent_id,
                    org_id,
                )
                if exists:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Only an org admin can remove admin-created allowlist entries.",
                    )
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
            await _insert_allowlist_event(
                conn,
                org_id=org_id,
                agent_id=agent_id,
                agent_url=row["agent_url"],
                event_type="deleted",
                source=row["source"],
                max_cost_usdc=row["max_cost_usdc"],
                actor_id=str(payload.get("sub", "")),
            )
    return JSONResponse(content={"deleted": agent_id})


class A2ADelegationEvent(BaseModel):
    id: str
    run_id: str
    agent_url: str
    agent_name: str | None = None
    task_status: str
    task_type: str
    cost_usdc: int
    billing_method: str
    settlement_tx: str | None = None
    error: str | None = None
    delivery_status: str = "not_attempted"
    delivery_resolved_at: str | None = Field(default=None, description="ISO 8601 timestamp; null while unresolved.")
    delivery_settlement_tx: str | None = None
    delivery_error: str | None = None
    created_at: str | None = Field(default=None, description="ISO 8601 timestamp; null if unavailable.")


@router.get("/a2a/delegations", tags=["A2A"], response_model=list[A2ADelegationEvent])
async def list_delegation_events(
    request: Request,
    limit: int = 50,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List delegation events for the authenticated org (newest first)."""
    from billing import get_delegation_events

    org_id: str = payload.get("org_id", payload["sub"])
    events = await get_delegation_events(org_id, limit=min(limit, 200))
    return JSONResponse(
        content=[
            {
                "id": e["id"],
                "run_id": e["run_id"],
                "agent_url": e["agent_url"],
                "agent_name": e["agent_name"],
                "task_status": e["task_status"],
                "task_type": e["task_type"],
                "cost_usdc": e["cost_usdc"],
                "billing_method": e["billing_method"],
                "settlement_tx": e["settlement_tx"],
                "error": e["error"],
                "delivery_status": e.get("delivery_status", "not_attempted"),
                "delivery_resolved_at": e["delivery_resolved_at"].isoformat() if e.get("delivery_resolved_at") else None,
                "delivery_settlement_tx": e.get("delivery_settlement_tx") or None,
                "delivery_error": e.get("delivery_error") or None,
                "created_at": e["created_at"].isoformat() if e["created_at"] else None,
            }
            for e in events
        ]
    )
