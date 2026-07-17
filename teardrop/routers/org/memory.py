# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Org-scoped persistent agent memory CRUD routes."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.memory import count_memories, delete_memory, list_memories, store_memory

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


# ─── Memory endpoints ────────────────────────────────────────────────────────


class StoreMemoryRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=500)


class MemoryListItem(BaseModel):
    id: str
    content: str
    source_run_id: str | None = None
    created_at: str = Field(..., description="ISO 8601 timestamp.")


class MemoryListResponse(BaseModel):
    items: list[MemoryListItem]
    total: int
    next_cursor: str | None = None


@router.get("/memories", tags=["Memory"], response_model=MemoryListResponse)
async def list_memories_endpoint(
    payload: dict = Depends(require_auth),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, description="ISO datetime cursor for pagination"),
) -> JSONResponse:
    """List memories for the authenticated org (newest first, cursor-paginated)."""
    org_id = _require_org_id(payload, "No org_id in token — memory requires an org-scoped credential.")

    from shared.pagination import parse_cursor

    cursor_dt = parse_cursor(cursor)
    entries = await list_memories(org_id, limit, cursor_dt)
    serialized = [
        {
            "id": e.id,
            "content": e.content,
            "source_run_id": e.source_run_id,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]
    next_cursor = serialized[-1]["created_at"] if serialized else None
    total = await count_memories(org_id)
    return JSONResponse(content={"items": serialized, "total": total, "next_cursor": next_cursor})


class MemoryCreatedResponse(BaseModel):
    id: str
    content: str
    created_at: str = Field(..., description="ISO 8601 timestamp.")


@router.post("/memories", tags=["Memory"], response_model=MemoryCreatedResponse, status_code=status.HTTP_201_CREATED)
async def store_memory_endpoint(
    body: StoreMemoryRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Manually store a memory for the authenticated org."""
    org_id = _require_org_id(payload, "No org_id in token — memory requires an org-scoped credential.")
    user_id: str = payload.get("sub", "")

    entry = await store_memory(org_id, user_id, body.content)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Failed to store memory — org limit may have been reached.",
        )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": entry.id,
            "content": entry.content,
            "created_at": entry.created_at.isoformat(),
        },
    )


class MemoryDeletedResponse(BaseModel):
    status: Literal["deleted"]


@router.delete("/memories/{memory_id}", tags=["Memory"], response_model=MemoryDeletedResponse)
async def delete_memory_endpoint(
    memory_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Delete a specific memory (org-scoped)."""
    org_id = _require_org_id(payload, "No org_id in token — memory requires an org-scoped credential.")
    deleted = await delete_memory(memory_id, org_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found.")
    return JSONResponse(content={"status": "deleted"})
