# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Org-scoped principal credit spend-limit management."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response, status
from pydantic import BaseModel, Field

from shared.db_pool import PgPool
from teardrop.dependencies import _require_org_id, require_org_admin

router = APIRouter()

PrincipalId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class PrincipalSpendLimitRequest(BaseModel):
    daily_limit_usdc: int = Field(..., gt=0, le=100_000_000)
    is_paused: bool = False


class PrincipalSpendLimitResponse(BaseModel):
    principal_id: str
    daily_limit_usdc: int
    is_paused: bool
    created_at: datetime
    updated_at: datetime


@router.get(
    "/org/principals/spend-limits",
    tags=["Organizations"],
    response_model=list[PrincipalSpendLimitResponse],
)
async def list_principal_spend_limits(
    request: Request,
    payload: dict = Depends(require_org_admin),
) -> list[dict]:
    org_id = _require_org_id(payload)
    pool: PgPool = request.app.state.pool
    rows = await pool.fetch(
        "SELECT principal_id, daily_limit_usdc, is_paused, created_at, updated_at "
        "FROM org_principal_spend_limits WHERE org_id = $1 ORDER BY principal_id",
        org_id,
    )
    return [dict(row) for row in rows]


@router.put(
    "/org/principals/{principal_id}/spend-limit",
    tags=["Organizations"],
    response_model=PrincipalSpendLimitResponse,
)
async def upsert_principal_spend_limit(
    request: Request,
    body: PrincipalSpendLimitRequest,
    principal_id: PrincipalId,
    payload: dict = Depends(require_org_admin),
) -> dict:
    org_id = _require_org_id(payload)
    pool: PgPool = request.app.state.pool
    row = await pool.fetchrow(
        """
        INSERT INTO org_principal_spend_limits
            (org_id, principal_id, daily_limit_usdc, is_paused)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (org_id, principal_id) DO UPDATE
            SET daily_limit_usdc = EXCLUDED.daily_limit_usdc,
                is_paused = EXCLUDED.is_paused,
                updated_at = NOW()
        RETURNING principal_id, daily_limit_usdc, is_paused, created_at, updated_at
        """,
        org_id,
        principal_id,
        body.daily_limit_usdc,
        body.is_paused,
    )
    return dict(row)


@router.delete(
    "/org/principals/{principal_id}/spend-limit",
    tags=["Organizations"],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_principal_spend_limit(
    request: Request,
    principal_id: PrincipalId,
    payload: dict = Depends(require_org_admin),
) -> Response:
    org_id = _require_org_id(payload)
    pool: PgPool = request.app.state.pool
    await pool.execute(
        "DELETE FROM org_principal_spend_limits WHERE org_id = $1 AND principal_id = $2",
        org_id,
        principal_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
