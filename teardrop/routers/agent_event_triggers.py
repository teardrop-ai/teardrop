# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Reactive event-triggered agent runs.

Two surfaces:

1. Org-scoped management CRUD under ``/agent/event-triggers`` (JWT auth). Each
   trigger stores a prompt template plus a per-trigger signing secret; only the
   SHA-256 hash of the secret is persisted and the plaintext is returned once.
2. A public inbound dispatch endpoint ``POST /agent/events/{trigger_token}``
   (no JWT). Callers authenticate with the per-trigger secret via the
   ``X-Teardrop-Trigger-Secret`` header (constant-time compared). The JSON body
   is interpolated into the prompt template and the agent runs in the
   background, billed through the standard credit ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from scheduling import (
    count_scheduled_runs,
    create_event_trigger,
    delete_scheduled_run,
    execute_event_run,
    get_event_trigger_for_dispatch,
    get_existing_dispatch,
    get_scheduled_run,
    list_event_triggers,
    list_scheduled_run_results,
    render_event_prompt,
    reserve_event_dispatch,
    rotate_event_trigger_secret,
    update_scheduled_run,
)
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.routers.agent_schedules import (
    ScheduleDeletedResponse,
    ScheduledRunResultsResponse,
    _validate_callback_url,
)

logger = logging.getLogger(__name__)

router = APIRouter()
settings = get_settings()

_MAX_EVENT_BODY_BYTES = 64 * 1024

# In-process back-pressure for inbound dispatch. The event loop is single
# threaded, so check-then-increment without an intervening await is atomic.
_inflight_event_runs = 0

# Strong references to in-flight background tasks. asyncio only holds weak
# references to tasks, so without this a dispatched run could be garbage
# collected mid-execution.
_pending_event_tasks: set[asyncio.Task] = set()


class CreateEventTriggerRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1, max_length=8_000)
    callback_url: str | None = Field(default=None, max_length=2048)


class UpdateEventTriggerRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=8_000)
    enabled: bool | None = None
    callback_url: str | None = Field(default=None, max_length=2048)


class EventTriggerItem(BaseModel):
    id: str
    org_id: str
    user_id: str
    name: str
    prompt: str
    schedule_kind: str
    enabled: bool
    callback_url: str | None = None
    trigger_token: str | None = None
    event_path: str | None = Field(default=None, description="'/agent/events/{trigger_token}'; null if unset.")
    consecutive_failures: int
    last_run_at: str | None = Field(default=None, description="ISO 8601 timestamp; null until first run.")
    created_at: str = Field(..., description="ISO 8601 timestamp.")
    updated_at: str = Field(..., description="ISO 8601 timestamp.")


class EventTriggerCreatedResponse(EventTriggerItem):
    secret: str = Field(..., description="Plaintext trigger secret — shown once, only its hash is persisted.")


class EventTriggerListResponse(BaseModel):
    items: list[EventTriggerItem]


class RotateSecretResponse(BaseModel):
    id: str
    secret: str = Field(..., description="New plaintext trigger secret — shown once, only its hash is persisted.")


class EventDispatchResponse(BaseModel):
    run_id: str
    status: Literal["accepted", "duplicate"]
    schedule_id: str
    result_path: str = Field(..., description="Path to fetch this run's result: /agent/event-triggers/{id}/runs.")


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _verify_secret(provided: str, stored_hash: str) -> bool:
    return hmac.compare_digest(_hash_secret(provided), stored_hash)


def _try_acquire_event_slot(limit: int) -> bool:
    global _inflight_event_runs
    if _inflight_event_runs >= limit:
        return False
    _inflight_event_runs += 1
    return True


def _release_event_slot() -> None:
    global _inflight_event_runs
    _inflight_event_runs = max(0, _inflight_event_runs - 1)


def _serialize_event_trigger(schedule) -> dict[str, object]:
    return {
        "id": schedule.id,
        "org_id": schedule.org_id,
        "user_id": schedule.user_id,
        "name": schedule.name,
        "prompt": schedule.prompt,
        "schedule_kind": schedule.schedule_kind,
        "enabled": schedule.enabled,
        "callback_url": schedule.callback_url,
        "trigger_token": schedule.trigger_token,
        "event_path": f"/agent/events/{schedule.trigger_token}" if schedule.trigger_token else None,
        "consecutive_failures": schedule.consecutive_failures,
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
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


async def _run_event_in_background(schedule, prompt: str, run_id: str) -> None:
    try:
        await execute_event_run(schedule, prompt=prompt, run_id=run_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("event run dispatch failed schedule_id=%s", schedule.id)
    finally:
        _release_event_slot()


def _require_enabled() -> None:
    if not settings.event_triggers_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event triggers are disabled.")


_NO_ORG = "No org_id in token — event triggers require an org-scoped credential."


@router.post(
    "/agent/event-triggers",
    tags=["Agent"],
    response_model=EventTriggerCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_event_trigger_endpoint(
    body: CreateEventTriggerRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    _require_enabled()
    org_id = _require_org_id(payload, _NO_ORG)
    user_id = str(payload.get("sub") or "")
    await _validate_callback_url(body.callback_url)
    if await count_scheduled_runs(org_id, schedule_kind="event") >= settings.event_triggers_max_per_org:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Event trigger limit reached for this organization.",
        )
    secret = secrets.token_urlsafe(32)
    trigger_token = secrets.token_urlsafe(24)
    schedule = await create_event_trigger(
        org_id=org_id,
        user_id=user_id,
        name=body.name,
        prompt=body.prompt,
        callback_url=body.callback_url,
        trigger_token=trigger_token,
        secret_hash=_hash_secret(secret),
    )
    content = _serialize_event_trigger(schedule)
    # Plaintext secret is returned exactly once; only its hash is persisted.
    content["secret"] = secret
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=content)


@router.get("/agent/event-triggers", tags=["Agent"], response_model=EventTriggerListResponse)
async def list_event_triggers_endpoint(payload: dict = Depends(require_auth)) -> JSONResponse:
    _require_enabled()
    org_id = _require_org_id(payload, _NO_ORG)
    triggers = await list_event_triggers(org_id)
    return JSONResponse(content={"items": [_serialize_event_trigger(t) for t in triggers]})


async def _get_owned_event_trigger(schedule_id: str, org_id: str):
    schedule = await get_scheduled_run(schedule_id, org_id)
    if schedule is None or schedule.schedule_kind != "event":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event trigger not found.")
    return schedule


@router.get("/agent/event-triggers/{schedule_id}", tags=["Agent"], response_model=EventTriggerItem)
async def get_event_trigger_endpoint(schedule_id: str, payload: dict = Depends(require_auth)) -> JSONResponse:
    _require_enabled()
    org_id = _require_org_id(payload, _NO_ORG)
    schedule = await _get_owned_event_trigger(schedule_id, org_id)
    return JSONResponse(content=_serialize_event_trigger(schedule))


@router.patch("/agent/event-triggers/{schedule_id}", tags=["Agent"], response_model=EventTriggerItem)
async def update_event_trigger_endpoint(
    schedule_id: str,
    body: UpdateEventTriggerRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    _require_enabled()
    org_id = _require_org_id(payload, _NO_ORG)
    await _get_owned_event_trigger(schedule_id, org_id)
    update_fields = body.model_fields_set
    if "callback_url" in update_fields:
        await _validate_callback_url(body.callback_url)
    update_kwargs: dict[str, object] = {}
    for field in ("name", "prompt", "enabled", "callback_url"):
        if field in update_fields:
            update_kwargs[field] = getattr(body, field)
    schedule = await update_scheduled_run(schedule_id, org_id, **update_kwargs)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event trigger not found.")
    return JSONResponse(content=_serialize_event_trigger(schedule))


@router.delete("/agent/event-triggers/{schedule_id}", tags=["Agent"], response_model=ScheduleDeletedResponse)
async def delete_event_trigger_endpoint(schedule_id: str, payload: dict = Depends(require_auth)) -> JSONResponse:
    _require_enabled()
    org_id = _require_org_id(payload, _NO_ORG)
    await _get_owned_event_trigger(schedule_id, org_id)
    deleted = await delete_scheduled_run(schedule_id, org_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event trigger not found.")
    return JSONResponse(content={"status": "deleted"})


@router.post("/agent/event-triggers/{schedule_id}/rotate-secret", tags=["Agent"], response_model=RotateSecretResponse)
async def rotate_event_trigger_secret_endpoint(
    schedule_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    _require_enabled()
    org_id = _require_org_id(payload, _NO_ORG)
    await _get_owned_event_trigger(schedule_id, org_id)
    secret = secrets.token_urlsafe(32)
    rotated = await rotate_event_trigger_secret(schedule_id, org_id, _hash_secret(secret))
    if not rotated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event trigger not found.")
    return JSONResponse(content={"id": schedule_id, "secret": secret})


@router.get("/agent/event-triggers/{schedule_id}/runs", tags=["Agent"], response_model=ScheduledRunResultsResponse)
async def list_event_trigger_runs(
    schedule_id: str,
    payload: dict = Depends(require_auth),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, description="ISO datetime cursor for pagination"),
) -> JSONResponse:
    _require_enabled()
    org_id = _require_org_id(payload, _NO_ORG)
    await _get_owned_event_trigger(schedule_id, org_id)
    from shared.pagination import parse_cursor  # noqa: PLC0415

    cursor_dt = parse_cursor(cursor)
    results = await list_scheduled_run_results(schedule_id, org_id, limit=limit, cursor=cursor_dt)
    serialized = [_serialize_result(result) for result in results]
    next_cursor = serialized[-1]["created_at"] if len(serialized) == limit else None
    return JSONResponse(content={"items": serialized, "next_cursor": next_cursor})


@router.post("/agent/events/{trigger_token}", tags=["Agent"], response_model=EventDispatchResponse)
async def dispatch_event(
    trigger_token: str,
    request: Request,
    x_teardrop_trigger_secret: str | None = Header(default=None),
    x_idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    """Public inbound webhook. Authenticated by the per-trigger secret header."""
    _require_enabled()

    found = await get_event_trigger_for_dispatch(trigger_token)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event trigger not found.")
    schedule, secret_hash = found

    if not secret_hash or not x_teardrop_trigger_secret or not _verify_secret(x_teardrop_trigger_secret, secret_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid trigger secret.")
    if not schedule.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event trigger not found.")

    raw = await request.body()
    if len(raw) > _MAX_EVENT_BODY_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Event payload too large.")
    try:
        body = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, ValueError):
        body = {}

    idempotency_key = x_idempotency_key
    if not idempotency_key and isinstance(body, dict):
        candidate = body.get("idempotency_key")
        idempotency_key = candidate if isinstance(candidate, str) and candidate else None

    rendered = render_event_prompt(schedule.prompt, body, max_chars=settings.event_triggers_prompt_max_chars)
    if not rendered.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Rendered prompt is empty after interpolation.",
        )

    result_path = f"/agent/event-triggers/{schedule.id}/runs"

    # Fast-path duplicate detection before consuming a concurrency slot.
    if idempotency_key:
        existing = await get_existing_dispatch(schedule.id, idempotency_key)
        if existing is not None:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"run_id": existing, "status": "duplicate", "schedule_id": schedule.id, "result_path": result_path},
            )

    if not _try_acquire_event_slot(settings.event_triggers_max_concurrency):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many concurrent event runs; retry later.",
        )

    run_id = str(uuid.uuid4())
    try:
        # Insert-first reservation closes the race the fast-path check cannot.
        if idempotency_key:
            reserved_run_id, is_new = await reserve_event_dispatch(schedule.id, idempotency_key, run_id)
            if not is_new:
                _release_event_slot()
                return JSONResponse(
                    status_code=status.HTTP_200_OK,
                    content={
                        "run_id": reserved_run_id,
                        "status": "duplicate",
                        "schedule_id": schedule.id,
                        "result_path": result_path,
                    },
                )
            run_id = reserved_run_id
        # Background task owns slot release from here on. Hold a strong reference
        # so the loop cannot garbage-collect the run before it finishes.
        task = asyncio.create_task(_run_event_in_background(schedule, rendered, run_id))
        _pending_event_tasks.add(task)
        task.add_done_callback(_pending_event_tasks.discard)
    except Exception:
        _release_event_slot()
        raise

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"run_id": run_id, "status": "accepted", "schedule_id": schedule.id, "result_path": result_path},
    )
