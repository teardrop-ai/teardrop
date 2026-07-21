# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace earnings sweep worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from marketplace.catalog import get_author_config
from marketplace.context import _get_pool
from marketplace.withdrawals import process_withdrawal
from teardrop.config import get_settings

logger = logging.getLogger(__name__)

_REPUTATION_RECENCY_HALF_LIFE_DAYS = 14.0
_REPUTATION_FRESHNESS_HALF_LIFE_DAYS = 30.0
_REPUTATION_PRIOR_SUCCESSES = 4.0
_REPUTATION_PRIOR_SAMPLE_SIZE = 5.0
_REPUTATION_FRESHNESS_FLOOR = 0.75


def _sweep_withdrawal_id(org_id: str, epoch_hour: int) -> str:
    """Derive a deterministic withdrawal ID for a sweep cycle."""
    raw = hashlib.sha256(f"sweep:{org_id}:{epoch_hour}".encode()).digest()
    hex_str = raw[:16].hex()
    return f"{hex_str[:8]}-{hex_str[8:12]}-5{hex_str[13:16]}-{hex_str[16:20]}-{hex_str[20:32]}"


def _sweep_backoff_seconds(attempt: int) -> int:
    """Exponential backoff for failed sweep attempts."""
    return min(2**attempt * 60, 86_400)


async def marketplace_sweep_once() -> int:
    """Auto-create and settle withdrawals for all qualifying orgs."""
    pool = _get_pool()
    settings = get_settings()
    min_amount = settings.marketplace_minimum_withdrawal_usdc
    max_retries = settings.marketplace_max_sweep_retries
    now = datetime.now(timezone.utc)
    epoch_hour = int(now.timestamp()) // 3600

    rows = await pool.fetch(
        """
        SELECT e.org_id, SUM(e.author_share_usdc) AS total
        FROM tool_author_earnings e
        WHERE e.status = 'pending'
          AND NOT EXISTS (
              SELECT 1 FROM tool_author_withdrawals w
              WHERE w.org_id = e.org_id
                AND w.status IN ('pending', 'failed')
                AND (w.next_sweep_at IS NULL OR w.next_sweep_at > NOW())
          )
        GROUP BY e.org_id
        HAVING SUM(e.author_share_usdc) >= $1
        LIMIT 50
        """,
        min_amount,
    )

    processed = 0
    for r in rows:
        org_id = r["org_id"]
        total = int(r["total"])
        withdrawal_id = _sweep_withdrawal_id(org_id, epoch_hour)
        try:
            existing = await pool.fetchrow(
                "SELECT id, status FROM tool_author_withdrawals WHERE id = $1",
                withdrawal_id,
            )
            if existing is None:
                config = await get_author_config(org_id)
                if config is None:
                    logger.warning(
                        "marketplace_sweep: org=%s has no author config, skipping",
                        org_id,
                    )
                    continue
                await pool.execute(
                    """
                    INSERT INTO tool_author_withdrawals
                        (id, org_id, amount_usdc, wallet, status, created_at,
                         sweep_attempt_count, last_sweep_error, next_sweep_at)
                    VALUES ($1, $2, $3, $4, 'pending', NOW(), 0, '', NULL)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    withdrawal_id,
                    org_id,
                    total,
                    config.settlement_wallet,
                )
            elif existing["status"] == "settled":
                processed += 1
                continue

            result = await process_withdrawal(withdrawal_id)

            if result.status == "settled":
                processed += 1
                logger.info(
                    "marketplace_sweep: settled org=%s amount=%d tx=%s",
                    org_id,
                    total,
                    result.tx_hash,
                )
            else:
                row = await pool.fetchrow(
                    "SELECT sweep_attempt_count FROM tool_author_withdrawals WHERE id = $1",
                    withdrawal_id,
                )
                attempt = int(row["sweep_attempt_count"]) + 1 if row else 1
                error_detail = result.last_sweep_error or "CDP transfer failed"

                if attempt >= max_retries:
                    await pool.execute(
                        """
                        UPDATE tool_author_withdrawals
                        SET status = 'exhausted',
                            sweep_attempt_count = $2,
                            last_sweep_error = $3,
                            next_sweep_at = NULL
                        WHERE id = $1
                        """,
                        withdrawal_id,
                        attempt,
                        error_detail,
                    )
                    logger.error(
                        "marketplace_sweep: exhausted withdrawal_id=%s org=%s "
                        "amount_usdc=%d attempts=%d error=%s — manual intervention required",
                        withdrawal_id,
                        org_id,
                        total,
                        attempt,
                        error_detail,
                    )
                else:
                    backoff = _sweep_backoff_seconds(attempt)
                    await pool.execute(
                        """
                        UPDATE tool_author_withdrawals
                        SET sweep_attempt_count = $2,
                            last_sweep_error = $3,
                            next_sweep_at = NOW() + ($4 * INTERVAL '1 second')
                        WHERE id = $1
                        """,
                        withdrawal_id,
                        attempt,
                        error_detail,
                        backoff,
                    )
                    logger.warning(
                        "marketplace_sweep: failed org=%s attempt=%d/%d retry_in=%ds",
                        org_id,
                        attempt,
                        max_retries,
                        backoff,
                    )

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "marketplace_sweep: unexpected error org=%s: %s",
                org_id,
                exc,
                exc_info=True,
            )

    if settings.agent_wallet_enabled:
        try:
            from teardrop.agent_wallets import get_settlement_wallet_balance_usdc

            balance = await get_settlement_wallet_balance_usdc(chain_id=settings.marketplace_settlement_chain_id)
            warn_threshold = settings.marketplace_settlement_warn_threshold_usdc
            if balance < warn_threshold:
                logger.error(
                    "marketplace_sweep: settlement wallet below threshold — "
                    "balance_usdc=%d threshold_usdc=%d account=%s — top up required",
                    balance,
                    warn_threshold,
                    settings.marketplace_settlement_cdp_account,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("marketplace_sweep: could not check settlement balance: %s", exc)

    return processed


async def reputation_rollup_once() -> int:
    """Recompute tool-quality aggregates and reputation scores for the catalog.

    Reads the full ``tool_call_events`` ledger and *overwrites* (never
    increments) its derived metrics on ``marketplace_tool_call_stats`` — this
    makes the rollup idempotent and safe to re-run over the same data. Calls
    made by a community tool's author are excluded to prevent self-traffic
    from inflating its reputation. ``total_calls`` is owned exclusively by
    ``record_marketplace_tool_call`` and is never touched here.

    Events are joined to active marketplace listings before aggregation. This
    both resolves a community tool's author from its canonical record and
    prevents internal or unpublished tools from gaining public aggregates.
    Bare (unqualified) names are normalized to platform names for the join.

    v2 uses a 14-day exponential decay for quality and sample size, a
    Beta(4, 1) prior to prevent sparse data from appearing certain, and a
    30-day freshness factor with a 0.75 floor. Task-class success is stored
    internally per tool for later context-aware routing; it is not exposed in
    the public catalog because task labels are derived telemetry.

    Returns the number of tools upserted.
    """
    pool = _get_pool()
    rows = await pool.fetch(
        """
        WITH catalog_tools AS (
            SELECT
                o.slug || '/' || t.name AS qualified_tool_name,
                'community'::TEXT AS tool_type,
                t.org_id AS author_org_id
            FROM org_tools t
            JOIN orgs o ON o.id = t.org_id
            WHERE t.publish_as_mcp = TRUE
              AND t.is_active = TRUE
                            AND o.slug <> 'platform'
            UNION ALL
            SELECT
                'platform/' || p.tool_name AS qualified_tool_name,
                'platform'::TEXT AS tool_type,
                NULL::TEXT AS author_org_id
            FROM marketplace_platform_tools p
            WHERE p.is_active = TRUE
        ),
        normalized_events AS (
            SELECT
                CASE WHEN tool_name LIKE '%/%' THEN tool_name ELSE 'platform/' || tool_name END
                    AS qualified_tool_name,
                org_id,
                success,
                elapsed_ms,
                run_id,
                created_at
            FROM tool_call_events
        ),
        eligible_events AS (
            SELECT
                c.qualified_tool_name,
                c.tool_type,
                c.author_org_id,
                e.org_id,
                e.run_id,
                e.success,
                e.elapsed_ms,
                e.created_at,
                EXP(
                    -LN(2.0) * GREATEST(
                        0.0,
                        EXTRACT(EPOCH FROM (NOW() - e.created_at)) / 86400.0
                    ) / $1
                ) AS recency_weight
            FROM normalized_events e
            JOIN catalog_tools c USING (qualified_tool_name)
            WHERE c.author_org_id IS NULL OR e.org_id IS DISTINCT FROM c.author_org_id
        ),
        agg AS (
            SELECT
                qualified_tool_name,
                tool_type,
                COUNT(*) FILTER (WHERE success) AS successes,
                COUNT(*) FILTER (WHERE NOT success) AS failures,
                COALESCE(SUM(elapsed_ms), 0)::BIGINT AS total_latency_ms,
                COALESCE(SUM(recency_weight) FILTER (WHERE success), 0.0) AS weighted_successes,
                COALESCE(SUM(recency_weight), 0.0) AS weighted_sample_size,
                MAX(created_at) AS last_event_at
            FROM eligible_events
            GROUP BY 1, 2
        ),
        task_agg AS (
            SELECT
                e.qualified_tool_name,
                CASE
                    WHEN d.task_class IN (
                        'general', 'research', 'analysis', 'data_retrieval', 'coding', 'transaction', 'automation'
                    ) THEN d.task_class
                    WHEN d.task_class IN ('risk', 'liquidation_risk') THEN 'analysis'
                    WHEN d.task_class IN (
                        'portfolio_lookup', 'balance_lookup', 'price_lookup', 'defi_positions', 'lending_rates',
                        'yield_comparison', 'protocol_tvl', 'marketplace_discovery', 'weather'
                    ) THEN 'data_retrieval'
                    ELSE 'other'
                END AS task_class,
                COALESCE(SUM(e.recency_weight) FILTER (WHERE e.success), 0.0) AS weighted_successes,
                COALESCE(SUM(e.recency_weight), 0.0) AS weighted_sample_size
            FROM eligible_events e
            JOIN run_decisions d ON d.run_id = e.run_id AND d.org_id = e.org_id
            WHERE d.task_class <> ''
            GROUP BY 1, 2
        ),
        task_scores AS (
            SELECT
                qualified_tool_name,
                jsonb_object_agg(
                    task_class,
                    jsonb_build_object(
                        'success_rate', ROUND(
                            ((weighted_successes + $2) / (weighted_sample_size + $3))::NUMERIC,
                            6
                        ),
                        'sample_size', ROUND(weighted_sample_size::NUMERIC, 6)
                    )
                )::TEXT AS task_success
            FROM task_agg
            GROUP BY 1
        ),
        scored AS (
            SELECT
                a.qualified_tool_name,
                a.tool_type,
                a.failures,
                a.total_latency_ms,
                a.weighted_sample_size AS sample_size,
                a.weighted_sample_size / (a.weighted_sample_size + $3) AS confidence,
                EXP(
                    -LN(2.0) * GREATEST(
                        0.0,
                        EXTRACT(EPOCH FROM (NOW() - a.last_event_at)) / 86400.0
                    ) / $4
                ) AS freshness,
                (a.weighted_successes + $2) / (a.weighted_sample_size + $3) AS success_rate,
                LN(1 + a.weighted_sample_size) AS log_volume,
                COALESCE(t.task_success, '{}') AS task_success
            FROM agg a
            LEFT JOIN task_scores t USING (qualified_tool_name)
        )
        SELECT
            qualified_tool_name,
            tool_type,
            failures,
            total_latency_ms,
            sample_size,
            confidence,
            freshness,
            success_rate,
            task_success,
            CASE WHEN MAX(log_volume) OVER () = 0 THEN 0
                 ELSE log_volume / MAX(log_volume) OVER () END AS popularity_norm
        FROM scored
        """,
        _REPUTATION_RECENCY_HALF_LIFE_DAYS,
        _REPUTATION_PRIOR_SUCCESSES,
        _REPUTATION_PRIOR_SAMPLE_SIZE,
        _REPUTATION_FRESHNESS_HALF_LIFE_DAYS,
    )

    for row in rows:
        success_rate = float(row["success_rate"])
        popularity_norm = float(row["popularity_norm"])
        freshness = float(row["freshness"])
        reputation_score = round(
            (0.6 * success_rate + 0.4 * popularity_norm)
            * (_REPUTATION_FRESHNESS_FLOOR + (1 - _REPUTATION_FRESHNESS_FLOOR) * freshness),
            6,
        )
        task_success = row["task_success"]
        if not isinstance(task_success, str):
            task_success = json.dumps(task_success)
        await pool.execute(
            """
            INSERT INTO marketplace_tool_call_stats
                (qualified_tool_name, tool_type, total_failures, total_latency_ms, reputation_score,
                 reputation_sample_size, reputation_confidence, reputation_freshness, reputation_task_success,
                 updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, NOW())
            ON CONFLICT (qualified_tool_name) DO UPDATE
                SET total_failures = EXCLUDED.total_failures,
                    total_latency_ms = EXCLUDED.total_latency_ms,
                    reputation_score = EXCLUDED.reputation_score,
                    reputation_sample_size = EXCLUDED.reputation_sample_size,
                    reputation_confidence = EXCLUDED.reputation_confidence,
                    reputation_freshness = EXCLUDED.reputation_freshness,
                    reputation_task_success = EXCLUDED.reputation_task_success,
                    updated_at = EXCLUDED.updated_at
            """,
            row["qualified_tool_name"],
            row["tool_type"],
            int(row["failures"]),
            int(row["total_latency_ms"]),
            reputation_score,
            float(row["sample_size"]),
            float(row["confidence"]),
            freshness,
            task_success,
        )

    return len(rows)


async def _marketplace_sweep_loop() -> None:
    """Background task: periodically auto-process qualifying withdrawals."""
    settings = get_settings()
    interval = settings.marketplace_sweep_interval_seconds
    logger.info("marketplace_sweep_loop: started (interval=%ds)", interval)

    cron_monitor = None
    if getattr(settings, "sentry_dsn", ""):
        try:
            from sentry_sdk.crons import monitor as sentry_monitor

            minutes = max(1, interval // 60 or 1)
            monitor_config = {
                "schedule": {"type": "interval", "value": minutes, "unit": "minute"},
                "checkin_margin": max(2, minutes // 4 or 2),
                "max_runtime": max(2, minutes * 2),
                "failure_issue_threshold": 2,
                "recovery_threshold": 2,
            }

            def cron_monitor() -> Any:  # type: ignore[no-redef]
                return sentry_monitor(
                    monitor_slug="marketplace-sweep",
                    monitor_config=monitor_config,
                )

        except Exception:
            cron_monitor = None

    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("marketplace_sweep_loop: cancelled")
            raise
        cancel_exc: BaseException | None = None
        try:
            if cron_monitor is not None:
                with cron_monitor():
                    try:
                        settled = await marketplace_sweep_once()
                    except asyncio.CancelledError as exc:
                        cancel_exc = exc
                        settled = 0
            else:
                settled = await marketplace_sweep_once()

            if settled:
                logger.info("marketplace_sweep_loop: settled withdrawals=%d", settled)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("marketplace_sweep_loop: sweep failed")

        if cancel_exc is not None:
            raise cancel_exc
