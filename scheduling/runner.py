# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Execution helpers for unattended scheduled runs."""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlparse

import httpx

from billing import get_byok_platform_fee, get_current_pricing, verify_credit
from scheduling.crud import (
    mark_scheduled_run_failed,
    mark_scheduled_run_skipped,
    mark_scheduled_run_succeeded,
    record_scheduled_run_result,
)
from scheduling.models import ScheduledRun, ScheduledRunResult
from teardrop.agent_runtime import run_agent_once
from teardrop.config import get_settings
from teardrop.llm_config import get_org_llm_config_cached
from tools.definitions.http_fetch import async_validate_url, make_ssrf_safe_httpx_transport

logger = logging.getLogger(__name__)


async def _deliver_callback(callback_url: str, payload: dict[str, object], schedule_id: str) -> None:
    parsed = urlparse(callback_url)
    host = parsed.hostname or "unknown"
    if parsed.scheme != "https":
        logger.warning("scheduled callback skipped non-https schedule_id=%s host=%s", schedule_id, host)
        return
    ssrf_err = await async_validate_url(callback_url)
    if ssrf_err:
        logger.warning("scheduled callback SSRF blocked schedule_id=%s host=%s error=%s", schedule_id, host, ssrf_err)
        return

    try:
        async with httpx.AsyncClient(
            transport=make_ssrf_safe_httpx_transport(),
            timeout=5.0,
            follow_redirects=False,
        ) as client:
            resp = await client.post(callback_url, json=payload)
        if 300 <= resp.status_code < 400:
            logger.warning(
                "scheduled callback redirect rejected schedule_id=%s host=%s status=%s",
                schedule_id,
                host,
                resp.status_code,
            )
            return
        if resp.status_code >= 400:
            logger.warning(
                "scheduled callback failed schedule_id=%s host=%s status=%s",
                schedule_id,
                host,
                resp.status_code,
            )
    except Exception:
        logger.warning("scheduled callback dispatch failed schedule_id=%s host=%s", schedule_id, host, exc_info=True)


async def _run_and_record(
    schedule: ScheduledRun,
    *,
    prompt: str,
    run_id: str,
    thread_id: str,
    user_role: str,
    metadata: dict[str, object],
) -> ScheduledRunResult:
    """Shared execution core for both scheduled and event-triggered runs.

    Verifies credit, executes the agent via the source-agnostic ``run_agent_once``
    engine, records the result, updates failure/success state, and delivers the
    optional SSRF-checked callback. The only run-source-specific inputs are the
    resolved prompt, run identity, thread id, user role, and metadata.
    """
    settings = get_settings()
    org_llm_cfg = await get_org_llm_config_cached(schedule.org_id)
    is_byok = bool(org_llm_cfg and org_llm_cfg.is_byok)
    platform_fee = get_byok_platform_fee(is_byok)
    pricing = await get_current_pricing()
    default_min = pricing.run_price_usdc if pricing is not None else 0
    min_required = platform_fee if is_byok else max(default_min, settings.credit_min_run_reserve_usdc)
    billing = await verify_credit(schedule.org_id, min_required)

    if not billing.verified:
        result = await record_scheduled_run_result(
            schedule_id=schedule.id,
            org_id=schedule.org_id,
            run_id=run_id,
            status="skipped",
            output_text="",
            cost_usdc=0,
            error=billing.error,
        )
        await mark_scheduled_run_skipped(schedule.id)
        return result

    result = await run_agent_once(
        org_id=schedule.org_id,
        user_id=schedule.user_id,
        usage_user_id=schedule.user_id,
        usage_org_id=schedule.org_id,
        user_message=prompt,
        run_id=run_id,
        thread_id=thread_id,
        billing=billing,
        is_byok=is_byok,
        org_llm_cfg=org_llm_cfg,
        platform_fee=platform_fee,
        timeout_seconds=float(settings.scheduled_runs_execution_timeout_seconds),
        metadata=metadata,
        user_role=user_role,
        emit_ui=False,
    )
    error_text = result.output_text if result.task_state != "completed" else ""
    stored = await record_scheduled_run_result(
        schedule_id=schedule.id,
        org_id=schedule.org_id,
        run_id=run_id,
        status=result.task_state,
        output_text=result.output_text,
        cost_usdc=result.usage_event.cost_usdc,
        error=error_text,
    )
    if result.task_state == "completed":
        await mark_scheduled_run_succeeded(schedule.id)
    else:
        await mark_scheduled_run_failed(
            schedule.id,
            max_consecutive_failures=settings.scheduled_runs_max_consecutive_failures,
        )

    if schedule.callback_url:
        await _deliver_callback(
            schedule.callback_url,
            {
                "schedule_id": schedule.id,
                "run_id": run_id,
                "status": stored.status,
                "output_text": stored.output_text,
                "cost_usdc": stored.cost_usdc,
                "error": stored.error,
                "created_at": stored.created_at.isoformat(),
            },
            schedule.id,
        )
    return stored


async def execute_scheduled_run(schedule: ScheduledRun) -> ScheduledRunResult:
    run_id = str(uuid.uuid4())
    return await _run_and_record(
        schedule,
        prompt=schedule.prompt,
        run_id=run_id,
        thread_id=f"scheduled:{schedule.id}:{run_id}",
        user_role="scheduled",
        metadata={"scheduled_run_id": schedule.id, "scheduled_run_name": schedule.name},
    )


async def execute_event_run(schedule: ScheduledRun, *, prompt: str, run_id: str) -> ScheduledRunResult:
    """Execute a reactive event-triggered run with a pre-rendered prompt and a
    caller-reserved ``run_id`` (so inbound idempotency holds across retries)."""
    return await _run_and_record(
        schedule,
        prompt=prompt,
        run_id=run_id,
        thread_id=f"event:{schedule.id}:{run_id}",
        user_role="event",
        metadata={"event_trigger_id": schedule.id, "event_trigger_name": schedule.name},
    )
