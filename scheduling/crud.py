# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Scheduled run + event trigger CRUD and worker-facing queries."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from scheduling.context import _get_pool
from scheduling.models import ScheduledRun, ScheduledRunResult
from shared.audit import insert_event_row

logger = logging.getLogger(__name__)

_UNSET = object()

# Canonical column projection for the ``scheduled_runs`` table. Kept in one place
# so interval schedules and event triggers map identically onto ``ScheduledRun``.
# ``secret_hash`` is deliberately excluded — it is only selected by the dispatch
# lookup and popped before model construction so it is never serialized.
_RUN_COLUMNS = (
    "id, org_id, user_id, name, prompt, schedule_kind, interval_seconds, "
    "cron_expr, enabled, callback_url, callback_format, trigger_token, next_run_at, last_run_at, "
    "consecutive_failures, created_at, updated_at"
)
_RUN_COLUMNS_SR = ", ".join(f"sr.{col.strip()}" for col in _RUN_COLUMNS.split(","))

_EVENT_TRIGGER_EVENT_INSERT_SQL = (
    "INSERT INTO event_trigger_events"
    " (id, schedule_id, org_id, run_id, event_type, status, cost_usdc, error)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
)

_EVENT_LEASE_LOCK_KEY = 0x54445250


@dataclass(frozen=True, slots=True)
class EventDispatchLeaseResult:
    run_id: str
    outcome: str


def _row_to_scheduled_run(row: Any) -> ScheduledRun:
    return ScheduledRun(**dict(row))


def _row_to_scheduled_run_result(row: Any) -> ScheduledRunResult:
    return ScheduledRunResult(**dict(row))


async def record_event_trigger_event(
    *,
    schedule_id: str,
    org_id: str,
    event_type: str,
    run_id: str = "",
    status: str = "",
    cost_usdc: int = 0,
    error: str = "",
) -> None:
    """Record bounded event-trigger metadata without affecting the main operation."""
    try:
        pool = _get_pool()
        await insert_event_row(
            pool,
            insert_sql=_EVENT_TRIGGER_EVENT_INSERT_SQL,
            values=(
                schedule_id,
                org_id,
                run_id,
                event_type,
                status,
                max(0, cost_usdc),
                error[:1024],
            ),
        )
    except Exception:
        logger.warning(
            "Event-trigger audit unavailable schedule_id=%s run_id=%s event_type=%s",
            schedule_id,
            run_id,
            event_type,
        )


async def count_scheduled_runs(org_id: str, schedule_kind: str = "interval") -> int:
    pool = _get_pool()
    value = await pool.fetchval(
        "SELECT COUNT(*) FROM scheduled_runs WHERE org_id = $1 AND schedule_kind = $2",
        org_id,
        schedule_kind,
    )
    return int(value or 0)


async def create_scheduled_run(
    *,
    org_id: str,
    user_id: str,
    name: str,
    prompt: str,
    interval_seconds: int,
    callback_url: str | None,
    callback_format: str = "json",
    first_run_at: datetime | None = None,
) -> ScheduledRun:
    pool = _get_pool()
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    next_run_at = first_run_at or now + timedelta(seconds=interval_seconds)
    row = await pool.fetchrow(
        f"""
        INSERT INTO scheduled_runs (
            id, org_id, user_id, name, prompt, schedule_kind, interval_seconds,
            cron_expr, enabled, callback_url, callback_format, next_run_at, created_at, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, 'interval', $6, NULL, TRUE, $7, $8, $9, $10, $10)
        RETURNING {_RUN_COLUMNS}
        """,
        run_id,
        org_id,
        user_id,
        name,
        prompt,
        interval_seconds,
        callback_url,
        callback_format,
        next_run_at,
        now,
    )
    return _row_to_scheduled_run(row)


async def create_event_trigger(
    *,
    org_id: str,
    user_id: str,
    name: str,
    prompt: str,
    callback_url: str | None,
    trigger_token: str,
    secret_hash: str,
    callback_format: str = "json",
) -> ScheduledRun:
    """Insert an event-triggered run. Interval/next_run columns stay NULL so the
    polling worker (which filters ``schedule_kind = 'interval'``) ignores it."""
    pool = _get_pool()
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    row = await pool.fetchrow(
        f"""
        INSERT INTO scheduled_runs (
            id, org_id, user_id, name, prompt, schedule_kind, interval_seconds,
            cron_expr, enabled, callback_url, callback_format, trigger_token,
            secret_hash, next_run_at, created_at, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, 'event', NULL, NULL, TRUE, $6, $7, $8, $9, NULL, $10, $10)
        RETURNING {_RUN_COLUMNS}
        """,
        run_id,
        org_id,
        user_id,
        name,
        prompt,
        callback_url,
        callback_format,
        trigger_token,
        secret_hash,
        now,
    )
    return _row_to_scheduled_run(row)


async def list_scheduled_runs(org_id: str) -> list[ScheduledRun]:
    pool = _get_pool()
    rows = await pool.fetch(
        f"""
        SELECT {_RUN_COLUMNS}
        FROM scheduled_runs
        WHERE org_id = $1 AND schedule_kind = 'interval'
        ORDER BY created_at DESC, id DESC
        """,
        org_id,
    )
    return [_row_to_scheduled_run(row) for row in rows]


async def list_event_triggers(org_id: str) -> list[ScheduledRun]:
    pool = _get_pool()
    rows = await pool.fetch(
        f"""
        SELECT {_RUN_COLUMNS}
        FROM scheduled_runs
        WHERE org_id = $1 AND schedule_kind = 'event'
        ORDER BY created_at DESC, id DESC
        """,
        org_id,
    )
    return [_row_to_scheduled_run(row) for row in rows]


async def get_scheduled_run(schedule_id: str, org_id: str) -> ScheduledRun | None:
    pool = _get_pool()
    row = await pool.fetchrow(
        f"""
        SELECT {_RUN_COLUMNS}
        FROM scheduled_runs
        WHERE id = $1 AND org_id = $2
        """,
        schedule_id,
        org_id,
    )
    return _row_to_scheduled_run(row) if row is not None else None


async def get_event_trigger_for_dispatch(trigger_token: str) -> tuple[ScheduledRun, str | None] | None:
    """Resolve an inbound trigger token to its row plus the stored secret hash.

    Returns ``None`` when no event trigger matches. The secret hash is returned
    separately and never embedded in the serialized model.
    """
    pool = _get_pool()
    row = await pool.fetchrow(
        f"""
        SELECT {_RUN_COLUMNS}, secret_hash
        FROM scheduled_runs
        WHERE trigger_token = $1 AND schedule_kind = 'event'
        """,
        trigger_token,
    )
    if row is None:
        return None
    data = dict(row)
    secret_hash = data.pop("secret_hash", None)
    return _row_to_scheduled_run(data), secret_hash


async def update_scheduled_run(
    schedule_id: str,
    org_id: str,
    *,
    name: str | object = _UNSET,
    prompt: str | object = _UNSET,
    interval_seconds: int | object = _UNSET,
    enabled: bool | object = _UNSET,
    callback_url: str | None | object = _UNSET,
    callback_format: str | object = _UNSET,
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
        updates.append(f"next_run_at = NOW() + ({interval_placeholder}::int * INTERVAL '1 second')")
    if enabled is not _UNSET:
        enabled_placeholder = _add(enabled)
        updates.append(f"enabled = {enabled_placeholder}")
        if enabled is True and interval_seconds is _UNSET:
            updates.append(
                "next_run_at = CASE WHEN schedule_kind = 'interval' "
                "THEN NOW() + (interval_seconds * INTERVAL '1 second') ELSE next_run_at END"
            )
    if callback_url is not _UNSET:
        updates.append(f"callback_url = {_add(callback_url)}")
    if callback_format is not _UNSET:
        updates.append(f"callback_format = {_add(callback_format)}")

    if not updates:
        return await get_scheduled_run(schedule_id, org_id)

    updates.append("updated_at = NOW()")
    row = await pool.fetchrow(
        f"""
        UPDATE scheduled_runs
        SET {", ".join(updates)}
        WHERE id = $1 AND org_id = $2
        RETURNING {_RUN_COLUMNS}
        """,
        *params,
    )
    return _row_to_scheduled_run(row) if row is not None else None


async def queue_scheduled_run_now(schedule_id: str, org_id: str) -> ScheduledRun | None:
    pool = _get_pool()
    row = await pool.fetchrow(
        f"""
        UPDATE scheduled_runs
        SET next_run_at = NOW(), updated_at = NOW()
        WHERE id = $1 AND org_id = $2
          AND schedule_kind = 'interval' AND enabled = TRUE
        RETURNING {_RUN_COLUMNS}
        """,
        schedule_id,
        org_id,
    )
    return _row_to_scheduled_run(row) if row is not None else None


async def rotate_event_trigger_secret(schedule_id: str, org_id: str, secret_hash: str) -> bool:
    """Replace the stored secret hash for an event trigger. Returns False when no
    matching event trigger exists for the org."""
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        UPDATE scheduled_runs
        SET secret_hash = $3, updated_at = NOW()
        WHERE id = $1 AND org_id = $2 AND schedule_kind = 'event'
        RETURNING id
        """,
        schedule_id,
        org_id,
        secret_hash,
    )
    return row is not None


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


async def get_scheduled_run_result(
    schedule_id: str,
    org_id: str,
    run_id: str,
) -> ScheduledRunResult | None:
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, schedule_id, org_id, run_id, status, output_text, cost_usdc, error, created_at
        FROM scheduled_run_results
        WHERE schedule_id = $1 AND org_id = $2 AND run_id = $3
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        schedule_id,
        org_id,
        run_id,
    )
    return _row_to_scheduled_run_result(row) if row is not None else None


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


async def reserve_event_dispatch(schedule_id: str, idempotency_key: str, run_id: str) -> tuple[str, bool]:
    """Insert-first idempotency reservation for an inbound event dispatch.

    Returns ``(run_id, True)`` when the reservation is newly created (caller
    should execute), or ``(existing_run_id, False)`` when the key was already
    reserved (caller should treat as a duplicate and skip execution).
    """
    pool = _get_pool()
    inserted = await pool.fetchval(
        """
        INSERT INTO event_dispatch_keys (schedule_id, idempotency_key, run_id, created_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (schedule_id, idempotency_key) DO NOTHING
        RETURNING run_id
        """,
        schedule_id,
        idempotency_key,
        run_id,
    )
    if inserted is not None:
        return str(inserted), True
    existing = await pool.fetchval(
        "SELECT run_id FROM event_dispatch_keys WHERE schedule_id = $1 AND idempotency_key = $2",
        schedule_id,
        idempotency_key,
    )
    return (str(existing) if existing is not None else run_id), False


async def reserve_event_dispatch_lease(
    *,
    schedule_id: str,
    org_id: str,
    idempotency_key: str,
    run_id: str,
    owner_id: str,
    lease_seconds: int,
    global_limit: int,
    org_limit: int,
) -> EventDispatchLeaseResult:
    """Atomically reserve an idempotent run and a cluster-wide execution slot."""
    pool = _get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", _EVENT_LEASE_LOCK_KEY)
            existing = await connection.fetchval(
                "SELECT run_id FROM event_dispatch_keys WHERE schedule_id = $1 AND idempotency_key = $2",
                schedule_id,
                idempotency_key,
            )
            if existing is not None:
                return EventDispatchLeaseResult(str(existing), "duplicate")

            active_global = await connection.fetchval(
                """
                SELECT COUNT(*) FROM event_dispatch_leases
                WHERE status IN ('reserved', 'running') AND lease_expires_at > NOW()
                """
            )
            if int(active_global or 0) >= global_limit:
                return EventDispatchLeaseResult(run_id, "saturated")

            active_org = await connection.fetchval(
                """
                SELECT COUNT(*) FROM event_dispatch_leases
                WHERE org_id = $1
                  AND status IN ('reserved', 'running')
                  AND lease_expires_at > NOW()
                """,
                org_id,
            )
            if int(active_org or 0) >= org_limit:
                return EventDispatchLeaseResult(run_id, "saturated")

            await connection.execute(
                """
                INSERT INTO event_dispatch_keys (schedule_id, idempotency_key, run_id, created_at)
                VALUES ($1, $2, $3, NOW())
                """,
                schedule_id,
                idempotency_key,
                run_id,
            )
            await connection.execute(
                """
                INSERT INTO event_dispatch_leases (
                    run_id, schedule_id, org_id, idempotency_key, owner_id,
                    status, lease_expires_at
                )
                VALUES ($1, $2, $3, $4, $5, 'reserved',
                        NOW() + make_interval(secs => $6))
                """,
                run_id,
                schedule_id,
                org_id,
                idempotency_key,
                owner_id,
                lease_seconds,
            )
            await insert_event_row(
                connection,
                insert_sql=_EVENT_TRIGGER_EVENT_INSERT_SQL,
                values=(schedule_id, org_id, run_id, "dispatch_accepted", "", 0, ""),
            )
            return EventDispatchLeaseResult(run_id, "accepted")


async def start_event_dispatch_lease(run_id: str, owner_id: str, lease_seconds: int) -> bool:
    pool = _get_pool()
    result = await pool.execute(
        """
        UPDATE event_dispatch_leases
        SET status = 'running', started_at = COALESCE(started_at, NOW()),
            lease_expires_at = NOW() + make_interval(secs => $3), updated_at = NOW()
        WHERE run_id = $1 AND owner_id = $2 AND status = 'reserved'
        """,
        run_id,
        owner_id,
        lease_seconds,
    )
    return result.endswith("1")


async def renew_event_dispatch_lease(run_id: str, owner_id: str, lease_seconds: int) -> bool:
    """Extend an owned running lease without changing its lifecycle state."""
    pool = _get_pool()
    result = await pool.execute(
        """
        UPDATE event_dispatch_leases
        SET lease_expires_at = NOW() + make_interval(secs => $3), updated_at = NOW()
        WHERE run_id = $1 AND owner_id = $2 AND status = 'running'
        """,
        run_id,
        owner_id,
        lease_seconds,
    )
    return result.endswith("1")


async def finish_event_dispatch_lease(
    *,
    schedule_id: str,
    org_id: str,
    run_id: str,
    owner_id: str,
    result: ScheduledRunResult,
) -> bool:
    """Commit the terminal lease state and immutable operational audit together."""
    terminal_status = "completed" if result.status == "completed" else "failed"
    pool = _get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            updated = await connection.fetchval(
                """
                UPDATE event_dispatch_leases
                SET status = $3, finished_at = NOW(), lease_expires_at = NOW(),
                    last_error = $4, updated_at = NOW()
                WHERE run_id = $1 AND owner_id = $2
                  AND status IN ('reserved', 'running')
                RETURNING run_id
                """,
                run_id,
                owner_id,
                terminal_status,
                ("Event run failed." if terminal_status == "failed" else ""),
            )
            if updated is None:
                return False
            await insert_event_row(
                connection,
                insert_sql=_EVENT_TRIGGER_EVENT_INSERT_SQL,
                values=(
                    schedule_id,
                    org_id,
                    run_id,
                    "run_settled",
                    result.status,
                    max(0, result.cost_usdc),
                    "Event run failed." if terminal_status == "failed" else "",
                ),
            )
            return True


async def fail_event_dispatch(
    *,
    schedule_id: str,
    org_id: str,
    run_id: str,
    owner_id: str,
    max_consecutive_failures: int,
    error: str = "Event run failed.",
) -> ScheduledRunResult | None:
    """Persist one sanitized failed result and terminal lease after an unexpected error."""
    pool = _get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            lease = await connection.fetchrow(
                """
                SELECT status FROM event_dispatch_leases
                WHERE run_id = $1 AND owner_id = $2
                FOR UPDATE
                """,
                run_id,
                owner_id,
            )
            if lease is None or lease["status"] not in {"reserved", "running"}:
                return None
            row = await connection.fetchrow(
                """
                SELECT id, schedule_id, org_id, run_id, status,
                       output_text, cost_usdc, error, created_at
                FROM scheduled_run_results
                WHERE schedule_id = $1 AND org_id = $2 AND run_id = $3
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                schedule_id,
                org_id,
                run_id,
            )
            if row is None:
                row = await connection.fetchrow(
                    """
                    INSERT INTO scheduled_run_results (
                        id, schedule_id, org_id, run_id, status, output_text,
                        cost_usdc, error, created_at
                    )
                    VALUES ($1, $2, $3, $4, 'failed', '', 0, $5, NOW())
                    ON CONFLICT (schedule_id, run_id) DO NOTHING
                    RETURNING id, schedule_id, org_id, run_id, status,
                              output_text, cost_usdc, error, created_at
                    """,
                    str(uuid.uuid4()),
                    schedule_id,
                    org_id,
                    run_id,
                    error[:1024],
                )
                if row is None:
                    row = await connection.fetchrow(
                        """
                        SELECT id, schedule_id, org_id, run_id, status,
                               output_text, cost_usdc, error, created_at
                        FROM scheduled_run_results
                        WHERE schedule_id = $1 AND org_id = $2 AND run_id = $3
                        """,
                        schedule_id,
                        org_id,
                        run_id,
                    )
                else:
                    await connection.execute(
                        """
                        UPDATE scheduled_runs
                        SET last_run_at = NOW(), consecutive_failures = consecutive_failures + 1,
                            enabled = CASE
                                WHEN consecutive_failures + 1 >= $2 THEN FALSE
                                ELSE enabled
                            END,
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        schedule_id,
                        max_consecutive_failures,
                    )
            stored = _row_to_scheduled_run_result(row)
            terminal_status = "completed" if stored.status == "completed" else "failed"
            await connection.execute(
                """
                UPDATE event_dispatch_leases
                SET status = $3, finished_at = NOW(), lease_expires_at = NOW(),
                    last_error = $4, updated_at = NOW()
                WHERE run_id = $1 AND owner_id = $2
                """,
                run_id,
                owner_id,
                terminal_status,
                "" if terminal_status == "completed" else error[:1024],
            )
            await insert_event_row(
                connection,
                insert_sql=_EVENT_TRIGGER_EVENT_INSERT_SQL,
                values=(schedule_id, org_id, run_id, "run_settled", stored.status, max(0, stored.cost_usdc), error[:1024]),
            )
            return stored


async def recover_expired_event_dispatches(*, limit: int = 100, max_consecutive_failures: int = 5) -> int:
    """Turn abandoned leases into terminal, pollable failures without re-execution."""
    pool = _get_pool()
    recovered = 0
    async with pool.acquire() as connection:
        async with connection.transaction():
            rows = await connection.fetch(
                """
                SELECT run_id, schedule_id, org_id, owner_id
                FROM event_dispatch_leases
                WHERE status IN ('reserved', 'running') AND lease_expires_at <= NOW()
                ORDER BY lease_expires_at, run_id
                LIMIT $1
                FOR UPDATE SKIP LOCKED
                """,
                limit,
            )
            for lease in rows:
                existing = await connection.fetchrow(
                    """
                    SELECT id, schedule_id, org_id, run_id, status,
                           output_text, cost_usdc, error, created_at
                    FROM scheduled_run_results
                    WHERE schedule_id = $1 AND org_id = $2 AND run_id = $3
                    """,
                    lease["schedule_id"],
                    lease["org_id"],
                    lease["run_id"],
                )
                if existing is None:
                    existing = await connection.fetchrow(
                        """
                        INSERT INTO scheduled_run_results (
                            id, schedule_id, org_id, run_id, status,
                            output_text, cost_usdc, error, created_at
                        )
                        VALUES ($1, $2, $3, $4, 'failed', '', 0,
                                'Event run recovery timed out.', NOW())
                        ON CONFLICT (schedule_id, run_id) DO NOTHING
                        RETURNING id, schedule_id, org_id, run_id, status,
                                  output_text, cost_usdc, error, created_at
                        """,
                        str(uuid.uuid4()),
                        lease["schedule_id"],
                        lease["org_id"],
                        lease["run_id"],
                    )
                    if existing is None:
                        existing = await connection.fetchrow(
                            """
                            SELECT id, schedule_id, org_id, run_id, status,
                                   output_text, cost_usdc, error, created_at
                            FROM scheduled_run_results
                            WHERE schedule_id = $1 AND org_id = $2 AND run_id = $3
                            """,
                            lease["schedule_id"],
                            lease["org_id"],
                            lease["run_id"],
                        )
                    else:
                        await connection.execute(
                            """
                            UPDATE scheduled_runs
                            SET last_run_at = NOW(), consecutive_failures = consecutive_failures + 1,
                                enabled = CASE
                                    WHEN consecutive_failures + 1 >= $2 THEN FALSE
                                    ELSE enabled
                                END,
                                updated_at = NOW()
                            WHERE id = $1
                            """,
                            lease["schedule_id"],
                            max_consecutive_failures,
                        )
                stored = _row_to_scheduled_run_result(existing)
                terminal_status = "completed" if stored.status == "completed" else "failed"
                await connection.execute(
                    """
                    UPDATE event_dispatch_leases
                    SET status = $2, finished_at = NOW(), lease_expires_at = NOW(),
                        last_error = $3, updated_at = NOW()
                    WHERE run_id = $1
                    """,
                    lease["run_id"],
                    terminal_status,
                    "" if terminal_status == "completed" else "Event run recovery timed out.",
                )
                await insert_event_row(
                    connection,
                    insert_sql=_EVENT_TRIGGER_EVENT_INSERT_SQL,
                    values=(
                        lease["schedule_id"],
                        lease["org_id"],
                        lease["run_id"],
                        "run_settled",
                        stored.status,
                        max(0, stored.cost_usdc),
                        "" if terminal_status == "completed" else "Event run recovery timed out.",
                    ),
                )
                recovered += 1
    return recovered


async def get_existing_dispatch(schedule_id: str, idempotency_key: str) -> str | None:
    pool = _get_pool()
    value = await pool.fetchval(
        "SELECT run_id FROM event_dispatch_keys WHERE schedule_id = $1 AND idempotency_key = $2",
        schedule_id,
        idempotency_key,
    )
    return str(value) if value is not None else None


async def event_dispatch_exists(schedule_id: str, run_id: str) -> bool:
    pool = _get_pool()
    value = await pool.fetchval(
        "SELECT 1 FROM event_dispatch_keys WHERE schedule_id = $1 AND run_id = $2 LIMIT 1",
        schedule_id,
        run_id,
    )
    return value is not None


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
        f"""
        UPDATE scheduled_runs
        SET last_run_at = NOW(),
            consecutive_failures = consecutive_failures + 1,
            enabled = CASE WHEN consecutive_failures + 1 >= $2 THEN FALSE ELSE enabled END,
            updated_at = NOW()
        WHERE id = $1
        RETURNING {_RUN_COLUMNS}
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
                f"""
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
                RETURNING {_RUN_COLUMNS_SR}
                """,
                limit,
            )
    return [_row_to_scheduled_run(row) for row in rows]
