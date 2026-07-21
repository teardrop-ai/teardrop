# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Retention sweeps for non-financial, non-ML operational data."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from teardrop.config import Settings, get_settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

_STALE_CHECKPOINT_THREADS_SQL = """
    SELECT thread_id
    FROM checkpoint_thread_activity
    WHERE last_activity_at < NOW() - make_interval(days => $1)
    ORDER BY last_activity_at
    LIMIT $2
    FOR UPDATE SKIP LOCKED
"""

_DELETE_SCHEDULED_RUN_RESULTS_SQL = """
    WITH candidates AS (
        SELECT ctid
        FROM scheduled_run_results
        WHERE created_at < NOW() - make_interval(days => $1)
        ORDER BY created_at
        LIMIT $2
        FOR UPDATE SKIP LOCKED
    ), deleted AS (
        DELETE FROM scheduled_run_results
        WHERE ctid IN (SELECT ctid FROM candidates)
        RETURNING 1
    )
    SELECT COUNT(*) FROM deleted
"""

_DELETE_ORG_TOOL_EXECUTION_EVENTS_SQL = """
    WITH candidates AS (
        SELECT ctid
        FROM org_tool_events
        WHERE event_type IN ('executed', 'failed')
          AND created_at < NOW() - make_interval(days => $1)
        ORDER BY created_at
        LIMIT $2
        FOR UPDATE SKIP LOCKED
    ), deleted AS (
        DELETE FROM org_tool_events
        WHERE ctid IN (SELECT ctid FROM candidates)
        RETURNING 1
    )
    SELECT COUNT(*) FROM deleted
"""

_DELETE_TELEMETRY_RUN_STARTS_SQL = """
    WITH candidates AS (
        SELECT ctid
        FROM telemetry_run_starts
        WHERE started_at < NOW() - make_interval(days => $1)
        ORDER BY started_at
        LIMIT $2
        FOR UPDATE SKIP LOCKED
    ), deleted AS (
        DELETE FROM telemetry_run_starts
        WHERE ctid IN (SELECT ctid FROM candidates)
        RETURNING 1
    )
    SELECT COUNT(*) FROM deleted
"""

_DELETE_EXPIRED_SIWE_SESSIONS_SQL = """
    WITH candidates AS (
        SELECT ctid
        FROM siwe_login_sessions
        WHERE expires_at < NOW()
        ORDER BY expires_at
        LIMIT $1
        FOR UPDATE SKIP LOCKED
    ), deleted AS (
        DELETE FROM siwe_login_sessions
        WHERE ctid IN (SELECT ctid FROM candidates)
        RETURNING 1
    )
    SELECT COUNT(*) FROM deleted
"""


@dataclass(frozen=True, slots=True)
class RetentionSweepResult:
    checkpoint_threads: int = 0
    scheduled_run_results: int = 0
    org_tool_execution_events: int = 0
    telemetry_run_starts: int = 0
    expired_siwe_login_sessions: int = 0

    @property
    def total_deleted(self) -> int:
        return (
            self.checkpoint_threads
            + self.scheduled_run_results
            + self.org_tool_execution_events
            + self.telemetry_run_starts
            + self.expired_siwe_login_sessions
        )


async def init_retention_db(pool: asyncpg.Pool) -> None:
    """Bind the shared application pool after migrations have completed."""
    global _pool
    _pool = pool


async def close_retention_db() -> None:
    """Release the shared pool reference before the application pool closes."""
    global _pool
    _pool = None


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Retention DB not initialised - call init_retention_db() first")
    return _pool


async def touch_checkpoint_thread(thread_id: str) -> None:
    """Record activity before a graph invocation without blocking an agent run."""
    if not thread_id or _pool is None:
        return
    try:
        await _pool.execute(
            """
            INSERT INTO checkpoint_thread_activity (thread_id, last_activity_at)
            VALUES ($1, NOW())
            ON CONFLICT (thread_id)
            DO UPDATE SET last_activity_at = EXCLUDED.last_activity_at
            """,
            thread_id,
        )
    except Exception:
        # Retention tracking must never prevent an agent response. Avoid
        # logging thread IDs because they include user-controlled identifiers.
        logger.warning("Checkpoint activity tracking unavailable")


async def _delete_stale_checkpoint_threads(
    pool: asyncpg.Pool,
    ttl_days: int,
    batch_size: int,
) -> int:
    """Delete whole inactive checkpoint threads while holding their activity locks.

    A concurrent ``touch_checkpoint_thread`` for an old thread blocks on the
    selected row until this transaction commits. It then records fresh activity
    after cleanup, so a new graph invocation cannot lose newly written state.
    """
    total_deleted = 0
    while True:
        async with pool.acquire() as connection:
            async with connection.transaction():
                rows = await connection.fetch(_STALE_CHECKPOINT_THREADS_SQL, ttl_days, batch_size)
                thread_ids = [str(row["thread_id"]) for row in rows]
                if not thread_ids:
                    return total_deleted

                await connection.execute("DELETE FROM checkpoint_writes WHERE thread_id = ANY($1::TEXT[])", thread_ids)
                await connection.execute("DELETE FROM checkpoint_blobs WHERE thread_id = ANY($1::TEXT[])", thread_ids)
                await connection.execute("DELETE FROM checkpoints WHERE thread_id = ANY($1::TEXT[])", thread_ids)
                await connection.execute(
                    "DELETE FROM checkpoint_thread_activity WHERE thread_id = ANY($1::TEXT[])",
                    thread_ids,
                )
                total_deleted += len(thread_ids)


async def _delete_ttl_rows(pool: asyncpg.Pool, delete_sql: str, ttl_days: int, batch_size: int) -> int:
    """Delete a bounded set of old rows until the table has no more candidates."""
    total_deleted = 0
    while True:
        deleted = int(await pool.fetchval(delete_sql, ttl_days, batch_size) or 0)
        total_deleted += deleted
        if deleted < batch_size:
            return total_deleted


async def _delete_expired_siwe_sessions(pool: asyncpg.Pool, batch_size: int) -> int:
    """Delete expired SIWE sessions, including their short-lived token material."""
    total_deleted = 0
    while True:
        deleted = int(await pool.fetchval(_DELETE_EXPIRED_SIWE_SESSIONS_SQL, batch_size) or 0)
        total_deleted += deleted
        if deleted < batch_size:
            return total_deleted


async def retention_sweep_once(runtime_settings: Settings | None = None) -> RetentionSweepResult:
    """Remove disposable records according to the configured retention periods.

    Financial ledgers, inbound A2A audit events, usage events, tool-call
    telemetry, and run decisions are intentionally absent from this module.
    """
    settings = runtime_settings or get_settings()
    pool = _get_pool()
    batch_size = settings.retention_sweep_batch_size

    checkpoint_threads = 0
    if settings.checkpoint_ttl_days > 0:
        checkpoint_threads = await _delete_stale_checkpoint_threads(
            pool,
            settings.checkpoint_ttl_days,
            batch_size,
        )

    scheduled_run_results = 0
    if settings.scheduled_run_results_ttl_days > 0:
        scheduled_run_results = await _delete_ttl_rows(
            pool,
            _DELETE_SCHEDULED_RUN_RESULTS_SQL,
            settings.scheduled_run_results_ttl_days,
            batch_size,
        )

    org_tool_execution_events = 0
    if settings.org_tool_execution_events_ttl_days > 0:
        org_tool_execution_events = await _delete_ttl_rows(
            pool,
            _DELETE_ORG_TOOL_EXECUTION_EVENTS_SQL,
            settings.org_tool_execution_events_ttl_days,
            batch_size,
        )

    telemetry_run_starts = 0
    if settings.telemetry_run_starts_ttl_days > 0:
        telemetry_run_starts = await _delete_ttl_rows(
            pool,
            _DELETE_TELEMETRY_RUN_STARTS_SQL,
            settings.telemetry_run_starts_ttl_days,
            batch_size,
        )

    expired_siwe_login_sessions = await _delete_expired_siwe_sessions(pool, batch_size)
    return RetentionSweepResult(
        checkpoint_threads=checkpoint_threads,
        scheduled_run_results=scheduled_run_results,
        org_tool_execution_events=org_tool_execution_events,
        telemetry_run_starts=telemetry_run_starts,
        expired_siwe_login_sessions=expired_siwe_login_sessions,
    )
