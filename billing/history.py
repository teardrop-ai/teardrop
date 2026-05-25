# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Billing history, invoices, and settlement-record helpers."""

from __future__ import annotations

import logging
from datetime import datetime

import sentry_sdk

from billing.context import _get_pool

logger = logging.getLogger(__name__)


async def record_settlement(
    usage_event_id: str,
    cost_usdc: int,
    settlement_tx: str,
    settlement_status: str = "settled",
) -> None:
    """Update a usage event with settlement details."""
    try:
        pool = _get_pool()
        await pool.execute(
            """
            UPDATE usage_events
            SET cost_usdc = $2, settlement_tx = $3, settlement_status = $4
            WHERE id = $1
            """,
            usage_event_id,
            cost_usdc,
            settlement_tx,
            settlement_status,
        )
    except Exception as exc:
        logger.exception("Failed to record settlement for event=%s", usage_event_id)
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("usage_event_id", str(usage_event_id))
            scope.set_tag("settlement_status", str(settlement_status))
            sentry_sdk.capture_exception(exc)


async def verify_settlement_on_chain(
    usage_event_id: str,
    tx_hash: str,
    chain_id: int,
) -> None:
    """Best-effort receipt check for x402 settlements after facilitator success."""
    if not tx_hash:
        return

    try:
        from teardrop.agent_wallets import verify_usdc_transfer  # noqa: PLC0415

        confirmed = await verify_usdc_transfer(tx_hash=tx_hash, chain_id=chain_id)
        if confirmed:
            return

        await record_settlement(usage_event_id, 0, tx_hash, "reverted")
        logger.error(
            "x402 settlement reverted on-chain usage_event_id=%s tx_hash=%s chain_id=%d",
            usage_event_id,
            tx_hash,
            chain_id,
        )
    except TimeoutError:
        logger.warning(
            "x402 settlement receipt check timed out usage_event_id=%s tx_hash=%s chain_id=%d",
            usage_event_id,
            tx_hash,
            chain_id,
        )
    except ValueError as exc:
        logger.warning(
            "x402 settlement receipt check skipped usage_event_id=%s tx_hash=%s chain_id=%d: %s",
            usage_event_id,
            tx_hash,
            chain_id,
            exc,
        )
    except Exception as exc:
        logger.exception(
            "x402 settlement receipt check failed usage_event_id=%s tx_hash=%s chain_id=%d",
            usage_event_id,
            tx_hash,
            chain_id,
        )
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("usage_event_id", str(usage_event_id))
            scope.set_tag("settlement_tx", str(tx_hash))
            scope.set_tag("chain_id", str(chain_id))
            sentry_sdk.capture_exception(exc)


async def get_billing_history(
    user_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[dict]:
    """Return recent settled usage events for a user."""
    pool = _get_pool()
    cursor_clause = "" if cursor is None else "AND created_at < $3"
    args: list = [user_id, limit, *([cursor] if cursor is not None else [])]
    rows = await pool.fetch(
        f"""
        SELECT id, run_id, tokens_in, tokens_out, tool_calls, duration_ms,
               cost_usdc, platform_fee_usdc, settlement_tx, settlement_status, created_at
        FROM usage_events
        WHERE user_id = $1 AND settlement_status != 'none'
          {cursor_clause}
        ORDER BY created_at DESC
        LIMIT $2
        """,
        *args,
    )
    return [dict(r) for r in rows]


async def get_revenue_summary(
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """Aggregate settled revenue within an optional date range."""
    pool = _get_pool()
    query = """
        SELECT COUNT(*) as total_settlements,
               COALESCE(SUM(cost_usdc), 0) as total_revenue_usdc
        FROM usage_events
        WHERE settlement_status = 'settled'
    """
    params: list = []
    idx = 1
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
        return {"total_settlements": 0, "total_revenue_usdc": 0}
    return dict(row)


async def get_invoices(
    user_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[dict]:
    """Return per-run invoice records for a user (all runs, not just settled)."""
    pool = _get_pool()
    cursor_clause = "" if cursor is None else "AND created_at < $3"
    args: list = [user_id, limit, *([cursor] if cursor is not None else [])]
    rows = await pool.fetch(
        f"""
        SELECT id, run_id, thread_id, tokens_in, tokens_out, tool_calls,
               tool_names, duration_ms, cost_usdc, platform_fee_usdc, settlement_tx,
               settlement_status, created_at
        FROM usage_events
        WHERE user_id = $1
          {cursor_clause}
        ORDER BY created_at DESC
        LIMIT $2
        """,
        *args,
    )
    return [dict(r) for r in rows]


async def get_invoice_by_run(run_id: str, user_id: str) -> dict | None:
    """Return a single run receipt, scoped to the authenticated user."""
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, run_id, thread_id, tokens_in, tokens_out, tool_calls,
               tool_names, duration_ms, cost_usdc, platform_fee_usdc, settlement_tx,
               settlement_status, created_at
        FROM usage_events
        WHERE run_id = $1 AND user_id = $2
        """,
        run_id,
        user_id,
    )
    return dict(row) if row is not None else None
