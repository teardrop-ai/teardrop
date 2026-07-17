# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Admin org memory inspection and purge.

All routes require the ``require_admin`` dependency. Extracted verbatim from
``teardrop.routers.admin`` with no logic changes.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from teardrop.dependencies import require_admin
from teardrop.memory import count_memories, delete_all_org_memories, list_memories

router = APIRouter()


class AdminMemoryItem(BaseModel):
    id: str
    content: str
    user_id: str
    source_run_id: str | None = None
    created_at: str = Field(..., description="ISO 8601 timestamp.")


class AdminMemoryListResponse(BaseModel):
    items: list[AdminMemoryItem]
    total: int


@router.get("/admin/memories/org/{org_id}", tags=["Admin", "Admin / Memory"], response_model=AdminMemoryListResponse)
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


class AdminMemoryPurgeResponse(BaseModel):
    status: Literal["purged"]
    deleted: int


@router.delete("/admin/memories/org/{org_id}", tags=["Admin", "Admin / Memory"], response_model=AdminMemoryPurgeResponse)
async def admin_purge_org_memories(
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: delete all memories for a specific org."""
    deleted_count = await delete_all_org_memories(org_id)
    return JSONResponse(content={"status": "purged", "deleted": deleted_count})
