# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Org-scoped A2A agent allowlist management and delegation history routes."""

from __future__ import annotations

import logging
import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from teardrop.config import get_settings
from teardrop.dependencies import require_auth, require_org_admin
from teardrop.rate_limit import _enforce_rate_limit

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


# ─── A2A Delegation – Org-scoped Agent Management ────────────────────────────


class OrgCreateA2AAgentRequest(BaseModel):
    agent_url: str = Field(..., min_length=10, max_length=2000)
    label: str | None = Field(default=None, max_length=200)
    max_cost_usdc: int = Field(default=0, description="Per-delegation cost cap in atomic USDC (0 = global default)")
    require_x402: bool = Field(default=False, description="Require x402 payment for this agent")
    jwt_forward: bool = Field(default=False, description="Forward caller JWT as Authorization header to this agent")


class OrgA2AAgentResponse(BaseModel):
    id: str
    org_id: str
    agent_url: str
    label: str | None = None
    max_cost_usdc: int
    require_x402: bool
    jwt_forward: bool


class OrgA2AAgentListItem(BaseModel):
    id: str
    agent_url: str
    label: str | None = None
    max_cost_usdc: int
    require_x402: bool
    jwt_forward: bool
    created_at: str | None = Field(default=None, description="ISO 8601 timestamp; null if unavailable.")


@router.post("/a2a/agents", tags=["A2A"], response_model=OrgA2AAgentResponse, status_code=status.HTTP_201_CREATED)
async def add_a2a_agent(
    request: Request,
    body: OrgCreateA2AAgentRequest,
    payload: dict = Depends(require_org_admin),
) -> JSONResponse:
    """Add a trusted A2A agent to the authenticated org's allowlist.

    Restricted to org admins: an allowlist entry exposes an arbitrary URL to the
    agent's delegate_to_agent tool (and, with jwt_forward, replays the caller's
    JWT to that endpoint), so registration is a privileged org-configuration
    action rather than a member-level one.
    """
    org_id: str = payload.get("org_id", payload["sub"])

    # Per-org rate limit — registering an allowlist entry exposes a URL to the
    # agent's delegate_to_agent tool, so cap the write rate to defend against a
    # stolen admin JWT bulk-injecting malicious endpoints.
    await _enforce_rate_limit(
        f"a2a_add:{org_id}",
        settings.rate_limit_auth_rpm,
        detail="Rate limit exceeded for A2A agent registration.",
    )

    pool: asyncpg.Pool = request.app.state.pool
    agent_id = str(uuid.uuid4())
    try:
        await pool.execute(
            """
            INSERT INTO a2a_allowed_agents (id, org_id, agent_url, label, max_cost_usdc, require_x402, jwt_forward)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            agent_id,
            org_id,
            body.agent_url.rstrip("/"),
            body.label,
            body.max_cost_usdc,
            body.require_x402,
            body.jwt_forward,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This agent URL is already in your allowlist",
        )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": agent_id,
            "org_id": org_id,
            "agent_url": body.agent_url,
            "label": body.label,
            "max_cost_usdc": body.max_cost_usdc,
            "require_x402": body.require_x402,
            "jwt_forward": body.jwt_forward,
        },
    )


@router.get("/a2a/agents", tags=["A2A"], response_model=list[OrgA2AAgentListItem])
async def list_a2a_agents(
    request: Request,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List all trusted A2A agents for the authenticated org."""
    org_id: str = payload.get("org_id", payload["sub"])
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


class OrgA2AAgentDeletedResponse(BaseModel):
    deleted: str = Field(..., description="The deleted agent's id.")


@router.delete("/a2a/agents/{agent_id}", tags=["A2A"], response_model=OrgA2AAgentDeletedResponse)
async def delete_a2a_agent(
    request: Request,
    agent_id: str,
    payload: dict = Depends(require_org_admin),
) -> JSONResponse:
    """Remove an A2A agent from the authenticated org's allowlist (org admins only)."""
    org_id: str = payload.get("org_id", payload["sub"])
    pool: asyncpg.Pool = request.app.state.pool
    result = await pool.execute(
        "DELETE FROM a2a_allowed_agents WHERE id = $1 AND org_id = $2",
        agent_id,
        org_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
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
                "created_at": e["created_at"].isoformat() if e["created_at"] else None,
            }
            for e in events
        ]
    )
