"""Usage accounting data layer (async Postgres via asyncpg).

Provides:
- UsageEvent model
- init_usage_db()       — create usage_events table on startup
- record_usage_event()  — async INSERT (fire-and-forget safe)
- get_usage_by_user()   — query usage for billing
- get_usage_by_org()    — query usage at org level
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import asyncpg
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ─── Models ───────────────────────────────────────────────────────────────────


class UsageEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    org_id: str
    thread_id: str
    run_id: str
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    tool_names: list[str] = Field(default_factory=list)
    duration_ms: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UsageSummary(BaseModel):
    """Aggregated usage totals for a date range."""
    total_runs: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_tool_calls: int = 0
    total_duration_ms: int = 0


# ─── Database initialisation ─────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def init_usage_db(pool: asyncpg.Pool) -> None:
    """Create usage_events table if it doesn't exist."""
    global _pool
    _pool = pool
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_events (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            org_id      TEXT NOT NULL,
            thread_id   TEXT NOT NULL,
            run_id      TEXT NOT NULL,
            tokens_in   INTEGER NOT NULL DEFAULT 0,
            tokens_out  INTEGER NOT NULL DEFAULT 0,
            tool_calls  INTEGER NOT NULL DEFAULT 0,
            tool_names  TEXT NOT NULL DEFAULT '[]',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMPTZ NOT NULL
        )
        """
    )
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_events (user_id, created_at)"
    )
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_org ON usage_events (org_id, created_at)"
    )
    logger.info("Usage tables ready (Postgres)")


async def close_usage_db() -> None:
    """Release the pool reference (pool is closed by the caller)."""
    global _pool
    if _pool is not None:
        _pool = None
        logger.info("Usage DB reference released")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Usage DB not initialised — call init_usage_db() first")
    return _pool


# ─── Write ────────────────────────────────────────────────────────────────────


async def record_usage_event(event: UsageEvent) -> None:
    """Insert a usage event. Logs errors but never raises — accounting must not block the SSE stream."""
    try:
        pool = _get_pool()
        await pool.execute(
            """
            INSERT INTO usage_events
                (id, user_id, org_id, thread_id, run_id, tokens_in, tokens_out,
                 tool_calls, tool_names, duration_ms, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            event.id,
            event.user_id,
            event.org_id,
            event.thread_id,
            event.run_id,
            event.tokens_in,
            event.tokens_out,
            event.tool_calls,
            json.dumps(event.tool_names),
            event.duration_ms,
            event.created_at,
        )
    except Exception:
        logger.exception("Failed to record usage event run_id=%s", event.run_id)


# ─── Read ─────────────────────────────────────────────────────────────────────


async def get_usage_by_user(
    user_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> UsageSummary:
    """Aggregate usage for a specific user within an optional date range."""
    return await _aggregate_usage("user_id", user_id, start, end)


async def get_usage_by_org(
    org_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> UsageSummary:
    """Aggregate usage for an entire org within an optional date range."""
    return await _aggregate_usage("org_id", org_id, start, end)


async def _aggregate_usage(
    column: str,
    value: str,
    start: datetime | None,
    end: datetime | None,
) -> UsageSummary:
    pool = _get_pool()
    # column is always a literal from our own code, never user input
    idx = 2
    query = f"""
        SELECT COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0),
               COALESCE(SUM(tool_calls),0), COALESCE(SUM(duration_ms),0)
        FROM usage_events
        WHERE {column} = $1
    """
    params: list = [value]
    if start is not None:
        query += f" AND created_at >= ${idx}"
        params.append(start)
        idx += 1
    if end is not None:
        query += f" AND created_at <= ${idx}"
        params.append(end)
        idx += 1

    row = await pool.fetchrow(query, *params)

    if row is None:
        return UsageSummary()
    return UsageSummary(
        total_runs=row[0],
        total_tokens_in=row[1],
        total_tokens_out=row[2],
        total_tool_calls=row[3],
        total_duration_ms=row[4],
    )
