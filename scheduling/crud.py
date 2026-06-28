# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Scheduled run CRUD and worker-facing queries."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from scheduling.context import _get_pool
from scheduling.models import ScheduledRun, ScheduledRunResult

_UNSET = object()


def _row_to_scheduled_run(row: Any) -> ScheduledRun:
    return ScheduledRun(**dict(row))


def _row_to_scheduled_run_result(row: Any) -> ScheduledRunResult:
    return ScheduledRunResult(**dict(row))


async def count_scheduled_runs(org_id: str) -> int:
    pool = _get_pool()
    value = await pool.fetchval("SELECT COUNT(*) FROM scheduled_runs WHERE org_id = $1", org_id)
    return int(value or 0)


async def create_scheduled_run(
    *,
    org_id: str,
    user_id: str,
    name: str,
    prompt: str,
    interval_seconds: int,
    callback_url: str | None,
) -> ScheduledRun:
    pool = _get_pool()
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    next_run_at = now + timedelta(seconds=interval_seconds)
    row = await pool.fetchrow(
        """
        INSERT INTO scheduled_runs (
            id, org_id, user_id, name, prompt, schedule_kind, interval_seconds,
            cron_expr, enabled, callback_url, next_run_at, created_at, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, 'interval', $6, NULL, TRUE, $7, $8, $9, $9)
        RETURNING id, org_id, user_id, name, prompt, schedule_kind, interval_seconds,
                  cron_expr, enabled, callback_url, next_run_at, last_run_at,
                  consecutive_failures, created_at, updated_at
        """,
        run_id,
        org_id,
        user_id,
        name,
        prompt,
        interval_seconds,
        callback_url,
        next_run_at,
        now,
    )
    return _row_to_scheduled_run(row)


async def list_scheduled_runs(org_id: str) -> list[ScheduledRun]:
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT id, org_id, user_id, name, prompt, schedule_kind, interval_seconds,
               cron_expr, enabled, callback_url, next_run_at, last_run_at,
               consecutive_failures, created_at, updated_at
        FROM scheduled_runs
        WHERE org_id = $1
        ORDER BY created_at DESC, id DESC
        """,
        org_id,
    )
    return [_row_to_scheduled_run(row) for row in rows]


async def get_scheduled_run(schedule_id: str, org_id: str) -> ScheduledRun | None:
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, org_id, user_id, name, prompt, schedule_kind, interval_seconds,
               cron_expr, enabled, callback_url, next_run_at, last_run_at,
               consecutive_failures, created_at, updated_at
        FROM scheduled_runs
        WHERE id = $1 AND org_id = $2
        """,
        schedule_id,
        org_id,
    )
    return _row_to_scheduled_run(row) if row is not None else None


async def update_scheduled_run(
    schedule_id: str,
    org_id: str,
    *,
    name: str | object = _UNSET,
    prompt: str | object = _UNSET,
    interval_seconds: int | object = _UNSET,
    enabled: bool | object = _UNSET,
    callback_url: str | None | object = _UNSET,
) -> ScheduledRun | None:
    pool = _get_pool()
    updates: list[str] = []
    params: list[Any] = [schedule_id, org_id]

    def _add(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if name is not _UNSET:
        updates.append(f"name = {_add(name)}")
    if prompt is not _UNSET:
        updates.append(f"prompt = {_add(prompt)}")
    if interval_seconds is not _UNSET:
        interval_placeholder = _add(interval_seconds)
        updates.append(f"interval_seconds = {interval_placeholder}")
        updates.append(f"next_run_at = NOW() + ({interval_placeholder} * INTERVAL '1 second')")
    if enabled is not _UNSET:
        enabled_placeholder = _add(enabled)
        updates.append(f"enabled = {enabled_placeholder}")
        if enabled is True and interval_seconds is _UNSET:
            updates.append("next_run_at = NOW() + (interval_seconds * INTERVAL '1 second')")
    if callback_url is not _UNSET:
        updates.append(f"callback_url = {_add(callback_url)}")

    if not updates:
        return await get_scheduled_run(schedule_id, org_id)

    updates.append("updated_at = NOW()")
    row = await pool.fetchrow(
        f"""
        UPDATE scheduled_runs
        SET {', '.join(updates)}
        WHERE id = $1 AND org_id = $2
        RETURNING id, org_id, user_id, name, prompt, schedule_kind, interval_seconds,
                  cron_expr, enabled, callback_url, next_run_at, last_run_at,
                  consecutive_failures, created_at, updated_at
        """,
        *params,
    )
    return _row_to_scheduled_run(row) if row is not None else None


async def delete_scheduled_run(schedule_id: str, org_id: str) -> bool:
    pool = _get_pool()
    result = await pool.execute("DELETE FROM scheduled_runs WHERE id = $1 AND org_id = $2", schedule_id, org_id)
    return result.endswith("1")


async def list_scheduled_run_results(
    schedule_id: str,
    org_id: str,
    *,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[ScheduledRunResult]:
    pool = _get_pool()
    params: list[Any] = [schedule_id, org_id, limit]
    cursor_clause = ""
    if cursor is not None:
        params.append(cursor)
        cursor_clause = f"AND created_at < ${len(params)}"
    rows = await pool.fetch(
        f"""
        SELECT id, schedule_id, org_id, run_id, status, output_text, cost_usdc, error, created_at
        FROM scheduled_run_results
        WHERE schedule_id = $1 AND org_id = $2 {cursor_clause}
        ORDER BY created_at DESC, id DESC
        LIMIT $3
        """,
        *params,
    )
    return [_row_to_scheduled_run_result(row) for row in rows]


async def record_scheduled_run_result(
    *,
    schedule_id: str,
    org_id: str,
    run_id: str,
    status: str,
    output_text: str,
    cost_usdc: int,
    error: str,
) -> ScheduledRunResult:
    pool = _get_pool()
    result_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    row = await pool.fetchrow(
        """
        INSERT INTO scheduled_run_results (
            id, schedule_id, org_id, run_id, status, output_text, cost_usdc, error, created_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id, schedule_id, org_id, run_id, status, output_text, cost_usdc, error, created_at
        """,
        result_id,
        schedule_id,
        org_id,
        run_id,
        status,
        output_text,
        cost_usdc,
        error,
        now,
    )
    return _row_to_scheduled_run_result(row)


async def mark_scheduled_run_skipped(schedule_id: str) -> None:
    pool = _get_pool()
    await pool.execute(
        """
        UPDATE scheduled_runs
        SET last_run_at = NOW(), updated_at = NOW()
        WHERE id = $1
        """,
        schedule_id,
    )


async def mark_scheduled_run_succeeded(schedule_id: str) -> None:
    pool = _get_pool()
    await pool.execute(
        """
        UPDATE scheduled_runs
        SET last_run_at = NOW(), consecutive_failures = 0, updated_at = NOW()
        WHERE id = $1
        """,
        schedule_id,
    )


async def mark_scheduled_run_failed(schedule_id: str, *, max_consecutive_failures: int) -> ScheduledRun | None:
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        UPDATE scheduled_runs
        SET last_run_at = NOW(),
            consecutive_failures = consecutive_failures + 1,
            enabled = CASE WHEN consecutive_failures + 1 >= $2 THEN FALSE ELSE enabled END,
            updated_at = NOW()
        WHERE id = $1
        RETURNING id, org_id, user_id, name, prompt, schedule_kind, interval_seconds,
                  cron_expr, enabled, callback_url, next_run_at, last_run_at,
                  consecutive_failures, created_at, updated_at
        """,
        schedule_id,
        max_consecutive_failures,
    )
    return _row_to_scheduled_run(row) if row is not None else None


async def claim_due_schedules(limit: int) -> list[ScheduledRun]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                WITH due AS (
                    SELECT id
                    FROM scheduled_runs
                    WHERE enabled = TRUE
                      AND schedule_kind = 'interval'
                      AND next_run_at <= NOW()
                    ORDER BY next_run_at ASC, id ASC
                    LIMIT $1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE scheduled_runs sr
                SET next_run_at = NOW() + (sr.interval_seconds * INTERVAL '1 second'),
                    updated_at = NOW()
                FROM due
                WHERE sr.id = due.id
                RETURNING sr.id, sr.org_id, sr.user_id, sr.name, sr.prompt, sr.schedule_kind,
                          sr.interval_seconds, sr.cron_expr, sr.enabled, sr.callback_url,
                          sr.next_run_at, sr.last_run_at, sr.consecutive_failures,
                          sr.created_at, sr.updated_at
                """,
                limit,
            )
    return [_row_to_scheduled_run(row) for row in rows]
