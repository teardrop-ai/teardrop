# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Usage accounting data layer (async Postgres).

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
from typing import Literal, cast

from pydantic import BaseModel, Field

from shared.db_pool import PgPool
from teardrop._meta import APP_VERSION

logger = logging.getLogger(__name__)

TOOL_CALL_EVENT_SCHEMA_VERSION = 1
TelemetryRunSource = Literal["api", "schedule", "trigger", "a2a"]
McpBillingMethod = Literal["x402", "credit"]
McpSettlementStatus = Literal["settled", "failed"]
_VALID_TELEMETRY_SOURCES = frozenset({"api", "schedule", "trigger", "a2a"})


def normalize_telemetry_source(source: str | None) -> TelemetryRunSource:
    """Return a database-safe run source, defaulting legacy callers to API."""
    if source not in _VALID_TELEMETRY_SOURCES:
        return "api"
    return cast(TelemetryRunSource, source)


# ─── Models ───────────────────────────────────────────────────────────────────


class UsageEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    org_id: str
    thread_id: str
    run_id: str
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    tool_calls: int = 0
    tool_names: list[str] = Field(default_factory=list)
    billable_tool_calls: int = 0
    billable_tool_names: list[str] = Field(default_factory=list)
    failed_tool_calls: int = 0
    failed_tool_names: list[str] = Field(default_factory=list)
    duration_ms: int = 0
    cost_usdc: int = 0
    platform_fee_usdc: int = 0
    settlement_tx: str = ""
    settlement_status: str = "none"
    provider: str = ""
    model: str = ""
    source: TelemetryRunSource = "api"
    runner_version: str = APP_VERSION
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UsageSummary(BaseModel):
    """Aggregated usage totals for a date range."""

    total_runs: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_tool_calls: int = 0
    total_duration_ms: int = 0


class TelemetryCompletenessBySource(BaseModel):
    source: TelemetryRunSource
    total_runs: int = 0
    usage_event_coverage: float = 0.0
    tool_eligible_runs: int = 0
    tool_event_coverage: float | None = None
    decision_coverage: float = 0.0
    outcome_label_coverage: float = 0.0


class TelemetryCompletenessResponse(BaseModel):
    window_days: int
    sources: list[TelemetryCompletenessBySource]


class MachineFunnelResponse(BaseModel):
    window_days: int
    machine_orgs_provisioned: int = 0
    siwe_orgs_provisioned: int = 0
    x402_orgs_provisioned: int = 0
    settlement_attempts: int = 0
    settled_calls: int = 0
    failed_calls: int = 0
    settlement_failure_rate: float | None = None
    settled_revenue_usdc: int = 0
    anonymous_settled_calls: int = 0
    org_bound_settled_calls: int = 0
    unique_anonymous_payers: int = 0
    converted_payers: int = 0
    wallet_conversion_rate: float | None = None
    repeat_payers: int = 0
    repeat_payer_rate: float | None = None
    repeat_payer_gate: bool = False


# ─── Database initialisation ─────────────────────────────────────────────────

_pool: PgPool | None = None


async def init_usage_db(pool: PgPool) -> None:
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
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            tool_calls  INTEGER NOT NULL DEFAULT 0,
            tool_names  TEXT NOT NULL DEFAULT '[]',
            billable_tool_calls INTEGER NOT NULL DEFAULT 0,
            billable_tool_names TEXT NOT NULL DEFAULT '[]',
            failed_tool_calls INTEGER NOT NULL DEFAULT 0,
            failed_tool_names TEXT NOT NULL DEFAULT '[]',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            source      TEXT NOT NULL DEFAULT 'api',
            runner_version TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ NOT NULL
        )
        """
    )
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_events (user_id, created_at)")
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_usage_org ON usage_events (org_id, created_at)")
    logger.info("Usage tables ready (Postgres)")


async def close_usage_db() -> None:
    """Release the pool reference (pool is closed by the caller)."""
    global _pool
    if _pool is not None:
        _pool = None
        logger.info("Usage DB reference released")


def _get_pool() -> PgPool:
    if _pool is None:
        raise RuntimeError("Usage DB not initialised — call init_usage_db() first")
    return _pool


# ─── Write ────────────────────────────────────────────────────────────────────


async def record_usage_event(event: UsageEvent) -> None:
    """Insert a usage event. Logs errors but never raises — accounting must not block the SSE
    stream."""
    try:
        pool = _get_pool()
        await pool.execute(
            """
            INSERT INTO usage_events
                (id, user_id, org_id, thread_id, run_id, tokens_in, tokens_out,
                                 cache_read_tokens, cache_creation_tokens,
                 tool_calls, tool_names, billable_tool_calls, billable_tool_names,
                 failed_tool_calls, failed_tool_names,
                 duration_ms, cost_usdc, platform_fee_usdc,
                 settlement_tx, settlement_status, provider, model, source, runner_version, created_at)
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25
            )
            """,
            event.id,
            event.user_id,
            event.org_id,
            event.thread_id,
            event.run_id,
            event.tokens_in,
            event.tokens_out,
            event.cache_read_tokens,
            event.cache_creation_tokens,
            event.tool_calls,
            json.dumps(event.tool_names),
            event.billable_tool_calls,
            json.dumps(event.billable_tool_names),
            event.failed_tool_calls,
            json.dumps(event.failed_tool_names),
            event.duration_ms,
            event.cost_usdc,
            event.platform_fee_usdc,
            event.settlement_tx,
            event.settlement_status,
            event.provider,
            event.model,
            event.source,
            event.runner_version,
            event.created_at,
        )
    except Exception:
        logger.exception("Failed to record usage event run_id=%s", event.run_id)


async def record_telemetry_run_started(
    run_id: str,
    org_id: str,
    source: TelemetryRunSource,
) -> None:
    """Persist a best-effort, immutable denominator for telemetry coverage."""
    if _pool is None:
        return
    try:
        source = normalize_telemetry_source(source)
        await _pool.execute(
            """
            INSERT INTO telemetry_run_starts (run_id, org_id, source, started_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (run_id) DO NOTHING
            """,
            run_id,
            org_id,
            source,
        )
    except Exception:
        logger.warning("Telemetry run-start recording unavailable")


async def record_mcp_call_event(
    event_id: str,
    org_id: str,
    payer_address: str,
    tool_name: str,
    billing_method: McpBillingMethod,
    cost_usdc: int,
    settlement_status: McpSettlementStatus,
    settlement_tx: str = "",
) -> None:
    """Persist an immutable MCP billing outcome without exposing payment material."""
    if _pool is None:
        return
    try:
        await _pool.execute(
            """
            INSERT INTO mcp_call_events
                (id, org_id, payer_address, tool_name, billing_method,
                 cost_usdc, settlement_status, settlement_tx, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            ON CONFLICT (id) DO NOTHING
            """,
            event_id,
            org_id,
            payer_address,
            tool_name,
            billing_method,
            cost_usdc,
            settlement_status,
            settlement_tx,
        )
    except Exception:
        logger.warning("MCP call event recording unavailable event_id=%s", event_id)


async def get_telemetry_completeness(days: int = 7) -> list[TelemetryCompletenessBySource]:
    """Return source-split post-run telemetry coverage over recent starts."""
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")

    pool = _get_pool()
    rows = await pool.fetch(
        """
        WITH recent_runs AS (
            SELECT run_id, source
            FROM telemetry_run_starts
            WHERE started_at >= NOW() - ($1 * INTERVAL '1 day')
        ),
        usage_records AS (
            SELECT DISTINCT ON (u.run_id) u.run_id, u.tool_calls
            FROM usage_events u
            JOIN recent_runs r ON r.run_id = u.run_id
            ORDER BY u.run_id, u.created_at DESC
        ),
        tool_event_counts AS (
            SELECT e.run_id, COUNT(*) AS tool_event_count
            FROM tool_call_events e
            JOIN recent_runs r ON r.run_id = e.run_id
            GROUP BY e.run_id
        ),
        decision_records AS (
            SELECT d.run_id, d.outcome_source
            FROM run_decisions d
            JOIN recent_runs r ON r.run_id = d.run_id
        )
        SELECT
            r.source,
            COUNT(*) AS total_runs,
            COUNT(u.run_id) AS usage_event_runs,
            COUNT(*) FILTER (WHERE COALESCE(u.tool_calls, 0) > 0) AS tool_eligible_runs,
            COUNT(*) FILTER (
                WHERE COALESCE(u.tool_calls, 0) > 0
                  AND COALESCE(t.tool_event_count, 0) >= u.tool_calls
            ) AS tool_event_runs,
            COUNT(d.run_id) AS decision_runs,
            COUNT(*) FILTER (WHERE d.outcome_source <> '') AS outcome_label_runs
        FROM recent_runs r
        LEFT JOIN usage_records u ON u.run_id = r.run_id
        LEFT JOIN tool_event_counts t ON t.run_id = r.run_id
        LEFT JOIN decision_records d ON d.run_id = r.run_id
        GROUP BY r.source
        ORDER BY r.source
        """,
        days,
    )

    result: list[TelemetryCompletenessBySource] = []
    for row in rows:
        total_runs = int(row["total_runs"])
        tool_eligible_runs = int(row["tool_eligible_runs"])
        result.append(
            TelemetryCompletenessBySource(
                source=row["source"],
                total_runs=total_runs,
                usage_event_coverage=round(int(row["usage_event_runs"]) / total_runs, 4),
                tool_eligible_runs=tool_eligible_runs,
                tool_event_coverage=(round(int(row["tool_event_runs"]) / tool_eligible_runs, 4) if tool_eligible_runs else None),
                decision_coverage=round(int(row["decision_runs"]) / total_runs, 4),
                outcome_label_coverage=round(int(row["outcome_label_runs"]) / total_runs, 4),
            )
        )
    return result


async def get_machine_funnel(days: int = 7) -> MachineFunnelResponse:
    """Derive machine acquisition, settlement, conversion, and retention signals."""
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")

    row = await _get_pool().fetchrow(
        """
        WITH window_calls AS (
            SELECT *
            FROM mcp_call_events
            WHERE created_at >= NOW() - ($1 * INTERVAL '1 day')
        ),
        anonymous_payers AS (
            SELECT payer_address, MIN(created_at) AS first_call_at, COUNT(*) AS settled_calls
            FROM window_calls
            WHERE settlement_status = 'settled'
              AND org_id = ''
              AND payer_address <> ''
            GROUP BY payer_address
        ),
        payer_conversions AS (
            SELECT DISTINCT p.payer_address
            FROM anonymous_payers p
            JOIN org_provisioning_events e
              ON LOWER(e.payer_address) = LOWER(p.payer_address)
             AND e.event_type = 'provisioned'
             AND e.created_at >= p.first_call_at
        ),
        recent_provisioning AS (
            SELECT DISTINCT org_id, method
            FROM org_provisioning_events
            WHERE event_type = 'provisioned'
              AND created_at >= NOW() - ($1 * INTERVAL '1 day')
        )
        SELECT
            (SELECT COUNT(*) FROM recent_provisioning) AS machine_orgs_provisioned,
            (SELECT COUNT(*) FROM recent_provisioning WHERE method = 'siwe') AS siwe_orgs_provisioned,
            (SELECT COUNT(*) FROM recent_provisioning WHERE method = 'x402') AS x402_orgs_provisioned,
            COUNT(*) AS settlement_attempts,
            COUNT(*) FILTER (WHERE settlement_status = 'settled') AS settled_calls,
            COUNT(*) FILTER (WHERE settlement_status = 'failed') AS failed_calls,
            COALESCE(SUM(cost_usdc) FILTER (WHERE settlement_status = 'settled'), 0) AS settled_revenue_usdc,
            COUNT(*) FILTER (WHERE settlement_status = 'settled' AND org_id = '') AS anonymous_settled_calls,
            COUNT(*) FILTER (WHERE settlement_status = 'settled' AND org_id <> '') AS org_bound_settled_calls,
            (SELECT COUNT(*) FROM anonymous_payers) AS unique_anonymous_payers,
            (SELECT COUNT(*) FROM payer_conversions) AS converted_payers,
            (SELECT COUNT(*) FROM anonymous_payers WHERE settled_calls > 1) AS repeat_payers
        FROM window_calls
        """,
        days,
    )

    settlement_attempts = int(row["settlement_attempts"])
    failed_calls = int(row["failed_calls"])
    unique_payers = int(row["unique_anonymous_payers"])
    converted_payers = int(row["converted_payers"])
    repeat_payers = int(row["repeat_payers"])
    return MachineFunnelResponse(
        window_days=days,
        machine_orgs_provisioned=int(row["machine_orgs_provisioned"]),
        siwe_orgs_provisioned=int(row["siwe_orgs_provisioned"]),
        x402_orgs_provisioned=int(row["x402_orgs_provisioned"]),
        settlement_attempts=settlement_attempts,
        settled_calls=int(row["settled_calls"]),
        failed_calls=failed_calls,
        settlement_failure_rate=(round(failed_calls / settlement_attempts, 4) if settlement_attempts else None),
        settled_revenue_usdc=int(row["settled_revenue_usdc"]),
        anonymous_settled_calls=int(row["anonymous_settled_calls"]),
        org_bound_settled_calls=int(row["org_bound_settled_calls"]),
        unique_anonymous_payers=unique_payers,
        converted_payers=converted_payers,
        wallet_conversion_rate=(round(converted_payers / unique_payers, 4) if unique_payers else None),
        repeat_payers=repeat_payers,
        repeat_payer_rate=(round(repeat_payers / unique_payers, 4) if unique_payers else None),
        repeat_payer_gate=repeat_payers > 0,
    )


async def record_tool_call_events(
    run_id: str,
    org_id: str,
    entries: list[dict],
    source: str = "api",
) -> None:
    """Insert per-tool-call telemetry rows. Logs errors but never raises — this is
    best-effort ML/observability telemetry and must not block the SSE stream.

    ``entries`` is the ``_tool_call_log`` accumulator built by
    ``agent.node_executor.tool_executor_node`` — each item has keys: tool_name,
    success, error_class, elapsed_ms, billable, args_hash. Raw tool arguments are
    never stored here, only the truncated hash already computed for within-run
    dedup (``agent.node_executor._call_signature``).
    """
    if not entries:
        return
    try:
        pool = _get_pool()
        source = normalize_telemetry_source(source)
        now = datetime.now(timezone.utc)
        await pool.executemany(
            """
            INSERT INTO tool_call_events
                (id, run_id, org_id, tool_name, success, error_class, elapsed_ms, billable, args_hash,
                 source, schema_version, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
            [
                (
                    str(uuid.uuid4()),
                    run_id,
                    org_id,
                    str(entry.get("tool_name", "")),
                    bool(entry.get("success", True)),
                    str(entry.get("error_class", "")),
                    int(entry.get("elapsed_ms", 0)),
                    bool(entry.get("billable", True)),
                    str(entry.get("args_hash", "")),
                    source,
                    TOOL_CALL_EVENT_SCHEMA_VERSION,
                    now,
                )
                for entry in entries
            ],
        )
    except Exception:
        logger.exception("Failed to record tool_call_events run_id=%s", run_id)


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
         SELECT COUNT(*) AS total_runs,
             COALESCE(SUM(tokens_in), 0) AS total_tokens_in,
             COALESCE(SUM(tokens_out), 0) AS total_tokens_out,
             COALESCE(SUM(tool_calls), 0) AS total_tool_calls,
             COALESCE(SUM(duration_ms), 0) AS total_duration_ms
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
        total_runs=row["total_runs"],
        total_tokens_in=row["total_tokens_in"],
        total_tokens_out=row["total_tokens_out"],
        total_tool_calls=row["total_tool_calls"],
        total_duration_ms=row["total_duration_ms"],
    )
