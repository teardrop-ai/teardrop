# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Org-scoped unattended scheduled agent runs."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from scheduling import (
    count_scheduled_runs,
    create_scheduled_run,
    delete_scheduled_run,
    get_scheduled_run,
    list_scheduled_run_results,
    list_scheduled_runs,
    update_scheduled_run,
)
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth

router = APIRouter()
settings = get_settings()


class CreateScheduledRunRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1, max_length=8_000)
    interval_seconds: int = Field(..., ge=1)
    callback_url: str | None = Field(default=None, max_length=2048)


class UpdateScheduledRunRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=8_000)
    interval_seconds: int | None = Field(default=None, ge=1)
    enabled: bool | None = None
    callback_url: str | None = Field(default=None, max_length=2048)


class ScheduledRunItem(BaseModel):
    id: str
    org_id: str
    user_id: str
    name: str
    prompt: str
    schedule_kind: str
    interval_seconds: int
    enabled: bool
    callback_url: str | None = None
    next_run_at: str = Field(..., description="ISO 8601 timestamp.")
    last_run_at: str | None = Field(default=None, description="ISO 8601 timestamp; null until first run.")
    consecutive_failures: int
    created_at: str = Field(..., description="ISO 8601 timestamp.")
    updated_at: str = Field(..., description="ISO 8601 timestamp.")


class ScheduledRunListResponse(BaseModel):
    items: list[ScheduledRunItem]


class ScheduledRunResultItem(BaseModel):
    id: str
    schedule_id: str
    org_id: str
    run_id: str
    status: str
    output_text: str | None = None
    cost_usdc: int
    error: str | None = None
    created_at: str = Field(..., description="ISO 8601 timestamp.")


class ScheduledRunResultsResponse(BaseModel):
    items: list[ScheduledRunResultItem]
    next_cursor: str | None = None


class ScheduleDeletedResponse(BaseModel):
    status: Literal["deleted"]


def _serialize_schedule(schedule) -> dict[str, object]:
    return {
        "id": schedule.id,
        "org_id": schedule.org_id,
        "user_id": schedule.user_id,
        "name": schedule.name,
        "prompt": schedule.prompt,
        "schedule_kind": schedule.schedule_kind,
        "interval_seconds": schedule.interval_seconds,
        "enabled": schedule.enabled,
        "callback_url": schedule.callback_url,
        "next_run_at": schedule.next_run_at.isoformat(),
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "consecutive_failures": schedule.consecutive_failures,
        "created_at": schedule.created_at.isoformat(),
        "updated_at": schedule.updated_at.isoformat(),
    }


def _serialize_result(result) -> dict[str, object]:
    return {
        "id": result.id,
        "schedule_id": result.schedule_id,
        "org_id": result.org_id,
        "run_id": result.run_id,
        "status": result.status,
        "output_text": result.output_text,
        "cost_usdc": result.cost_usdc,
        "error": result.error,
        "created_at": result.created_at.isoformat(),
    }


async def _validate_callback_url(callback_url: str | None) -> None:
    if not callback_url:
        return
    parsed = urlparse(callback_url)
    if parsed.scheme != "https":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="callback_url must use https.",
        )
    from tools.definitions.http_fetch import async_validate_url  # noqa: PLC0415

    ssrf_err = await async_validate_url(callback_url)
    if ssrf_err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsafe callback_url: {ssrf_err}",
        )


def _validate_interval(interval_seconds: int) -> None:
    minimum = get_settings().scheduled_runs_min_interval_seconds
    if interval_seconds < minimum:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"interval_seconds must be at least {minimum}.",
        )


@router.post("/agent/schedules", tags=["Agent"], response_model=ScheduledRunItem, status_code=status.HTTP_201_CREATED)
async def create_agent_schedule(
    body: CreateScheduledRunRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    if not settings.scheduled_runs_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled runs are disabled.")
    org_id = _require_org_id(payload, "No org_id in token — scheduled runs require an org-scoped credential.")
    user_id = str(payload.get("sub") or "")
    _validate_interval(body.interval_seconds)
    await _validate_callback_url(body.callback_url)
    if await count_scheduled_runs(org_id) >= settings.scheduled_runs_max_per_org:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Scheduled run limit reached for this organization.",
        )
    schedule = await create_scheduled_run(
        org_id=org_id,
        user_id=user_id,
        name=body.name,
        prompt=body.prompt,
        interval_seconds=body.interval_seconds,
        callback_url=body.callback_url,
    )
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=_serialize_schedule(schedule))


@router.get("/agent/schedules", tags=["Agent"], response_model=ScheduledRunListResponse)
async def list_agent_schedules(payload: dict = Depends(require_auth)) -> JSONResponse:
    if not settings.scheduled_runs_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled runs are disabled.")
    org_id = _require_org_id(payload, "No org_id in token — scheduled runs require an org-scoped credential.")
    schedules = await list_scheduled_runs(org_id)
    return JSONResponse(content={"items": [_serialize_schedule(schedule) for schedule in schedules]})


@router.get("/agent/schedules/{schedule_id}", tags=["Agent"], response_model=ScheduledRunItem)
async def get_agent_schedule(schedule_id: str, payload: dict = Depends(require_auth)) -> JSONResponse:
    if not settings.scheduled_runs_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled runs are disabled.")
    org_id = _require_org_id(payload, "No org_id in token — scheduled runs require an org-scoped credential.")
    schedule = await get_scheduled_run(schedule_id, org_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled run not found.")
    return JSONResponse(content=_serialize_schedule(schedule))


@router.patch("/agent/schedules/{schedule_id}", tags=["Agent"], response_model=ScheduledRunItem)
async def update_agent_schedule_endpoint(
    schedule_id: str,
    body: UpdateScheduledRunRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    if not settings.scheduled_runs_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled runs are disabled.")
    org_id = _require_org_id(payload, "No org_id in token — scheduled runs require an org-scoped credential.")
    update_fields = body.model_fields_set
    if "interval_seconds" in update_fields and body.interval_seconds is not None:
        _validate_interval(body.interval_seconds)
    if "callback_url" in update_fields:
        await _validate_callback_url(body.callback_url)
    update_kwargs: dict[str, object] = {}
    if "name" in update_fields:
        update_kwargs["name"] = body.name
    if "prompt" in update_fields:
        update_kwargs["prompt"] = body.prompt
    if "interval_seconds" in update_fields:
        update_kwargs["interval_seconds"] = body.interval_seconds
    if "enabled" in update_fields:
        update_kwargs["enabled"] = body.enabled
    if "callback_url" in update_fields:
        update_kwargs["callback_url"] = body.callback_url
    schedule = await update_scheduled_run(
        schedule_id,
        org_id,
        **update_kwargs,
    )
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled run not found.")
    return JSONResponse(content=_serialize_schedule(schedule))


@router.delete("/agent/schedules/{schedule_id}", tags=["Agent"], response_model=ScheduleDeletedResponse)
async def delete_agent_schedule_endpoint(schedule_id: str, payload: dict = Depends(require_auth)) -> JSONResponse:
    if not settings.scheduled_runs_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled runs are disabled.")
    org_id = _require_org_id(payload, "No org_id in token — scheduled runs require an org-scoped credential.")
    deleted = await delete_scheduled_run(schedule_id, org_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled run not found.")
    return JSONResponse(content={"status": "deleted"})


@router.get("/agent/schedules/{schedule_id}/runs", tags=["Agent"], response_model=ScheduledRunResultsResponse)
async def list_agent_schedule_runs(
    schedule_id: str,
    payload: dict = Depends(require_auth),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, description="ISO datetime cursor for pagination"),
) -> JSONResponse:
    if not settings.scheduled_runs_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled runs are disabled.")
    org_id = _require_org_id(payload, "No org_id in token — scheduled runs require an org-scoped credential.")
    schedule = await get_scheduled_run(schedule_id, org_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled run not found.")
    from shared.pagination import parse_cursor  # noqa: PLC0415

    cursor_dt = parse_cursor(cursor)
    results = await list_scheduled_run_results(schedule_id, org_id, limit=limit, cursor=cursor_dt)
    serialized = [_serialize_result(result) for result in results]
    next_cursor = serialized[-1]["created_at"] if len(serialized) == limit else None
    return JSONResponse(content={"items": serialized, "next_cursor": next_cursor})
