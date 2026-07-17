# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Admin A2A delegation allowlist management.

All routes require the ``require_admin`` dependency. Extracted verbatim from
``teardrop.routers.admin`` with no logic changes.
"""

from __future__ import annotations

import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from teardrop.dependencies import require_admin

router = APIRouter()


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


@router.get("/admin/a2a/agents/{org_id}", tags=["Admin", "Admin / A2A"], response_model=list[A2AAgentListItem])
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


class A2AAgentDeletedResponse(BaseModel):
    deleted: str = Field(..., description="The deleted agent's id.")


@router.delete("/admin/a2a/agents/{agent_id}", tags=["Admin", "Admin / A2A"], response_model=A2AAgentDeletedResponse)
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
