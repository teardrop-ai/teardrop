# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Admin usage reporting: per-user and per-org aggregated usage.

All routes require the ``require_admin`` dependency. Extracted verbatim from
``teardrop.routers.admin`` with no logic changes.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from teardrop.dependencies import require_admin
from teardrop.usage import UsageSummary, get_usage_by_org, get_usage_by_user

router = APIRouter()


@router.get("/admin/usage/{user_id}", tags=["Admin", "Admin / Usage"], response_model=UsageSummary)
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


@router.get("/admin/usage/org/{org_id}", tags=["Admin", "Admin / Usage"], response_model=UsageSummary)
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
