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
    event_dispatch_exists,
    execute_event_run,
    fail_event_dispatch,
    finish_event_dispatch_lease,
    get_event_trigger_for_dispatch,
    get_existing_dispatch,
    get_scheduled_run,
    get_scheduled_run_result,
    list_event_triggers,
    list_scheduled_run_results,
    record_event_trigger_event,
    render_event_prompt,
    renew_event_dispatch_lease,
    reserve_event_dispatch_lease,
    rotate_event_trigger_secret,
    start_event_dispatch_lease,
    update_scheduled_run,
)
from scheduling.a2a_tasks import EventTaskResponse, build_event_task
from shared.request_ip import client_ip_from_request
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.rate_limit import _enforce_rate_limit
from teardrop.routers.agent_schedules import (
    ScheduleDeletedResponse,
    ScheduledRunResultsResponse,
    _validate_callback_url,
)

logger = logging.getLogger(__name__)

router = APIRouter()
settings = get_settings()

_MAX_EVENT_BODY_BYTES = 64 * 1024
_MAX_IDEMPOTENCY_KEY_CHARS = 256
_EVENT_WORKER_ID = str(uuid.uuid4())

# Strong references to in-flight background tasks. asyncio only holds weak
# references to tasks, so without this a dispatched run could be garbage
# collected mid-execution.
_pending_event_tasks: set[asyncio.Task] = set()


class CreateEventTriggerRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1, max_length=12_000)
    callback_url: str | None = Field(default=None, max_length=2048)


class UpdateEventTriggerRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=12_000)
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
    result_path: str = Field(..., description="A2A task endpoint for this run.")


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _verify_secret(provided: str, stored_hash: str) -> bool:
    return hmac.compare_digest(_hash_secret(provided), stored_hash)


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


async def _renew_event_lease(run_id: str, lease_seconds: int) -> None:
    interval = max(5, lease_seconds // 3)
    while True:
        await asyncio.sleep(interval)
        renewed = await renew_event_dispatch_lease(run_id, _EVENT_WORKER_ID, lease_seconds)
        if not renewed:
            logger.error("Event lease renewal stopped run_id=%s", run_id)
            return


async def _run_event_in_background(schedule, prompt: str, run_id: str, lease_seconds: int) -> None:
    heartbeat: asyncio.Task | None = None
    try:
        if not await start_event_dispatch_lease(run_id, _EVENT_WORKER_ID, lease_seconds):
            await fail_event_dispatch(
                schedule_id=schedule.id,
                org_id=schedule.org_id,
                run_id=run_id,
                owner_id=_EVENT_WORKER_ID,
                max_consecutive_failures=settings.scheduled_runs_max_consecutive_failures,
            )
            return
        heartbeat = asyncio.create_task(_renew_event_lease(run_id, lease_seconds))
        result = await execute_event_run(schedule, prompt=prompt, run_id=run_id)
        await finish_event_dispatch_lease(
            schedule_id=schedule.id,
            org_id=schedule.org_id,
            run_id=run_id,
            owner_id=_EVENT_WORKER_ID,
            result=result,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        await fail_event_dispatch(
            schedule_id=schedule.id,
            org_id=schedule.org_id,
            run_id=run_id,
            owner_id=_EVENT_WORKER_ID,
            max_consecutive_failures=settings.scheduled_runs_max_consecutive_failures,
        )
        logger.error("Event run failed schedule_id=%s run_id=%s", schedule.id, run_id)
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass


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
    await record_event_trigger_event(
        schedule_id=schedule.id,
        org_id=org_id,
        event_type="trigger_created",
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
    await record_event_trigger_event(
        schedule_id=schedule.id,
        org_id=org_id,
        event_type="trigger_updated",
    )
    return JSONResponse(content=_serialize_event_trigger(schedule))


@router.delete("/agent/event-triggers/{schedule_id}", tags=["Agent"], response_model=ScheduleDeletedResponse)
async def delete_event_trigger_endpoint(schedule_id: str, payload: dict = Depends(require_auth)) -> JSONResponse:
    _require_enabled()
    org_id = _require_org_id(payload, _NO_ORG)
    await _get_owned_event_trigger(schedule_id, org_id)
    deleted = await delete_scheduled_run(schedule_id, org_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event trigger not found.")
    await record_event_trigger_event(
        schedule_id=schedule_id,
        org_id=org_id,
        event_type="trigger_deleted",
    )
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
    await record_event_trigger_event(
        schedule_id=schedule_id,
        org_id=org_id,
        event_type="secret_rotated",
    )
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


@router.get(
    "/agent/event-triggers/{schedule_id}/runs/{run_id}",
    tags=["Agent"],
    response_model=EventTaskResponse,
)
async def get_event_trigger_run(
    schedule_id: str,
    run_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    _require_enabled()
    org_id = _require_org_id(payload, _NO_ORG)
    schedule = await _get_owned_event_trigger(schedule_id, org_id)
    if not await event_dispatch_exists(schedule.id, run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event run not found.")
    result = await get_scheduled_run_result(schedule.id, org_id, run_id)
    task = build_event_task(schedule_id=schedule.id, run_id=run_id, result=result)
    return JSONResponse(content=task.model_dump(mode="json", by_alias=True))


@router.post("/agent/events/{trigger_token}", tags=["Agent"], response_model=EventDispatchResponse)
async def dispatch_event(
    trigger_token: str,
    request: Request,
    x_teardrop_trigger_secret: str | None = Header(default=None),
    x_idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    """Public inbound webhook. Authenticated by the per-trigger secret header."""
    _require_enabled()

    client_ip = client_ip_from_request(request, trusted_proxy_count=settings.trusted_proxy_count)
    if client_ip:
        await _enforce_rate_limit(
            f"event-trigger:ip:{client_ip}",
            settings.rate_limit_requests_per_minute,
            detail="Event dispatch rate limit exceeded.",
        )
    await _enforce_rate_limit(
        f"event-trigger:token:{_hash_secret(trigger_token)}",
        settings.rate_limit_agent_rpm,
        detail="Event dispatch rate limit exceeded.",
    )

    found = await get_event_trigger_for_dispatch(trigger_token)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event trigger not found.")
    schedule, secret_hash = found

    if not secret_hash or not x_teardrop_trigger_secret or not _verify_secret(x_teardrop_trigger_secret, secret_hash):
        await record_event_trigger_event(
            schedule_id=schedule.id,
            org_id=schedule.org_id,
            event_type="secret_rejected",
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid trigger secret.")
    if not schedule.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event trigger not found.")

    raw = await request.body()
    if len(raw) > _MAX_EVENT_BODY_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Event payload too large.")
    try:
        body = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Event payload must be valid JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Event payload must be a JSON object.")

    idempotency_key = x_idempotency_key
    if not idempotency_key and isinstance(body, dict):
        candidate = body.get("idempotency_key")
        idempotency_key = candidate if isinstance(candidate, str) and candidate else None
    if idempotency_key and len(idempotency_key) > _MAX_IDEMPOTENCY_KEY_CHARS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Idempotency key is too long.")

    rendered = render_event_prompt(schedule.prompt, body, max_chars=settings.event_triggers_prompt_max_chars)
    if not rendered.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Rendered prompt is empty after interpolation.",
        )

    # Fast-path duplicate detection before consuming a concurrency slot.
    if idempotency_key:
        existing = await get_existing_dispatch(schedule.id, idempotency_key)
        if existing is not None:
            await record_event_trigger_event(
                schedule_id=schedule.id,
                org_id=schedule.org_id,
                run_id=existing,
                event_type="dispatch_duplicate",
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "run_id": existing,
                    "status": "duplicate",
                    "schedule_id": schedule.id,
                    "result_path": f"/agent/event-triggers/{schedule.id}/runs/{existing}",
                },
            )

    run_id = str(uuid.uuid4())
    lease_seconds = int(settings.scheduled_runs_execution_timeout_seconds) + 60
    lease_reserved = False
    try:
        reservation_key = idempotency_key or f"run:{run_id}"
        reservation = await reserve_event_dispatch_lease(
            schedule_id=schedule.id,
            org_id=schedule.org_id,
            idempotency_key=reservation_key,
            run_id=run_id,
            owner_id=_EVENT_WORKER_ID,
            lease_seconds=lease_seconds,
            global_limit=settings.event_triggers_max_concurrency,
            org_limit=settings.event_triggers_max_concurrency_per_org,
        )
        if reservation.outcome == "saturated":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many concurrent event runs; retry later.",
            )
        if reservation.outcome == "duplicate":
            await record_event_trigger_event(
                schedule_id=schedule.id,
                org_id=schedule.org_id,
                run_id=reservation.run_id,
                event_type="dispatch_duplicate",
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "run_id": reservation.run_id,
                    "status": "duplicate",
                    "schedule_id": schedule.id,
                    "result_path": f"/agent/event-triggers/{schedule.id}/runs/{reservation.run_id}",
                },
            )
        run_id = reservation.run_id
        lease_reserved = True
        # Background task owns slot release from here on. Hold a strong reference
        # so the loop cannot garbage-collect the run before it finishes.
        task = asyncio.create_task(_run_event_in_background(schedule, rendered, run_id, lease_seconds))
        _pending_event_tasks.add(task)
        task.add_done_callback(_pending_event_tasks.discard)
    except Exception:
        if lease_reserved:
            await fail_event_dispatch(
                schedule_id=schedule.id,
                org_id=schedule.org_id,
                run_id=run_id,
                owner_id=_EVENT_WORKER_ID,
                max_consecutive_failures=settings.scheduled_runs_max_consecutive_failures,
            )
        raise

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "run_id": run_id,
            "status": "accepted",
            "schedule_id": schedule.id,
            "result_path": f"/agent/event-triggers/{schedule.id}/runs/{run_id}",
        },
    )
