# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Usage reporting routes."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from teardrop.dependencies import require_auth
from teardrop.usage import UsageSummary, get_usage_by_user

router = APIRouter()


@router.get("/usage/me", tags=["Usage"], response_model=UsageSummary)
async def usage_me(
    payload: dict = Depends(require_auth),
    start: str | None = None,
    end: str | None = None,
) -> JSONResponse:
    """Return aggregated usage for the authenticated user."""

    start_dt = datetime.fromisoformat(start) if start else None
    end_dt = datetime.fromisoformat(end) if end else None
    summary = await get_usage_by_user(payload["sub"], start_dt, end_dt)
    return JSONResponse(content=summary.model_dump())
