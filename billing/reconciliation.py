# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Read-only reconciliation of ``billing_charges`` against the legacy billing records.

Gate for moving revenue reads onto the unified ledger: over a window that starts
after migration 115 was deployed, every check must report zero discrepancies.
Delegation funding debits (``a2a_delegation ...``) are pass-through spend, not
charges, and are intentionally not compared.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from billing.context import _get_pool

MAX_WINDOW = timedelta(days=31)

# SQL must not contain a literal percent sign: the pool translates $N to psycopg %s.
_AGENT_SOURCES = "('api', 'schedule', 'trigger', 'a2a')"
_ALL_SOURCES = "('api', 'schedule', 'trigger', 'a2a', 'mcp', 'mcp_v1')"

_AGENT_RUNS_SQL = f"""
WITH runs AS (
    SELECT ue.id, ue.cost_usdc, ue.settlement_status, COALESCE(ue.settlement_tx, '') AS settlement_tx,
           c.id AS charge_id, c.status AS charge_status, c.settled_amount_usdc, c.settlement_tx AS charge_tx
    FROM usage_events ue
    LEFT JOIN billing_charges c
        ON c.usage_event_id = ue.id AND c.source IN {_AGENT_SOURCES}
    WHERE ue.created_at >= $1 AND ue.created_at < $2
      AND ue.settlement_status IN ('settled', 'failed', 'reverted')
), flagged AS (
    SELECT id,
           charge_id IS NULL AS missing_charge,
           charge_id IS NOT NULL AND charge_status <> settlement_status AS status_mismatch,
           charge_status = 'settled' AND settlement_status = 'settled'
               AND settled_amount_usdc <> cost_usdc AS amount_mismatch,
           charge_id IS NOT NULL AND charge_tx <> settlement_tx AS tx_mismatch
    FROM runs
)
SELECT COUNT(*) AS source_rows,
       COUNT(*) FILTER (WHERE missing_charge) AS missing_charge,
       COUNT(*) FILTER (WHERE status_mismatch) AS status_mismatch,
       COUNT(*) FILTER (WHERE amount_mismatch) AS amount_mismatch,
       COUNT(*) FILTER (WHERE tx_mismatch) AS tx_mismatch,
       (SELECT COUNT(*)
        FROM billing_charges oc
        LEFT JOIN usage_events x ON x.id = oc.usage_event_id
        WHERE oc.source IN {_AGENT_SOURCES}
          AND oc.created_at >= $1 AND oc.created_at < $2
          AND (x.id IS NULL OR x.settlement_status = 'none')) AS unmatched_charge,
       (array_agg(id ORDER BY id)
            FILTER (WHERE missing_charge OR status_mismatch OR amount_mismatch OR tx_mismatch))[1:5] AS sample_ids
FROM flagged
"""

_MCP_CALLS_SQL = """
WITH calls AS (
    SELECT e.id, e.cost_usdc, e.settlement_status, e.settlement_tx,
           c.id AS charge_id, c.status AS charge_status, c.amount_usdc, c.settlement_tx AS charge_tx,
           EXISTS (
               SELECT 1 FROM pending_settlements p WHERE p.charge_id = c.id AND p.status = 'settled'
           ) AS retried
    FROM mcp_call_events e
    LEFT JOIN billing_charges c ON c.source = 'mcp' AND c.invocation_id = e.id
    WHERE e.created_at >= $1 AND e.created_at < $2
), flagged AS (
    SELECT id,
           charge_id IS NULL AS missing_charge,
           settlement_status = 'failed' AND charge_status = 'settled' AND retried AS retry_settled,
           charge_id IS NOT NULL AND charge_status <> settlement_status
               AND NOT (settlement_status = 'failed' AND charge_status = 'settled' AND retried) AS status_mismatch,
           charge_id IS NOT NULL AND amount_usdc <> cost_usdc AS amount_mismatch,
           charge_id IS NOT NULL AND charge_tx <> settlement_tx AS tx_mismatch
    FROM calls
)
SELECT COUNT(*) AS source_rows,
       COUNT(*) FILTER (WHERE missing_charge) AS missing_charge,
       COUNT(*) FILTER (WHERE status_mismatch) AS status_mismatch,
       COUNT(*) FILTER (WHERE amount_mismatch) AS amount_mismatch,
       COUNT(*) FILTER (WHERE tx_mismatch) AS tx_mismatch,
       COUNT(*) FILTER (WHERE retry_settled) AS retry_settled,
       (SELECT COUNT(*)
        FROM billing_charges oc
        WHERE oc.source = 'mcp'
          AND oc.created_at >= $1 AND oc.created_at < $2
          AND NOT EXISTS (SELECT 1 FROM mcp_call_events x WHERE x.id = oc.invocation_id)) AS unmatched_charge,
       (array_agg(id ORDER BY id)
            FILTER (WHERE missing_charge OR status_mismatch OR amount_mismatch OR tx_mismatch))[1:5] AS sample_ids
FROM flagged
"""

# Credit retries debit with reason 'run:' || run_id, where MCP recoveries reuse the call id as run_id.
_RUN_DEBITS_SQL = f"""
WITH debits AS (
    SELECT l.id, l.org_id, l.amount_usdc, substring(l.reason FROM 5) AS invocation_id,
           COUNT(*) OVER (PARTITION BY l.org_id, l.reason) AS debit_count
    FROM org_credit_ledger l
    WHERE l.operation = 'debit' AND starts_with(l.reason, 'run:')
      AND l.created_at >= $1 AND l.created_at < $2
), flagged AS (
    SELECT d.id,
           c.id IS NULL AS missing_charge,
           c.id IS NOT NULL AND c.status <> 'settled' AS status_mismatch,
           c.id IS NOT NULL AND c.settled_amount_usdc <> d.amount_usdc AS amount_mismatch,
           d.debit_count > 1 AS duplicate_debit
    FROM debits d
    LEFT JOIN billing_charges c
        ON c.source IN {_ALL_SOURCES} AND c.invocation_id = d.invocation_id
       AND c.billing_method = 'credit' AND c.org_id = d.org_id
)
SELECT COUNT(*) AS source_rows,
       COUNT(*) FILTER (WHERE missing_charge) AS missing_charge,
       COUNT(*) FILTER (WHERE status_mismatch) AS status_mismatch,
       COUNT(*) FILTER (WHERE amount_mismatch) AS amount_mismatch,
       COUNT(*) FILTER (WHERE duplicate_debit) AS duplicate_debit,
       (SELECT COUNT(*)
        FROM billing_charges c
        WHERE c.source IN {_AGENT_SOURCES} AND c.billing_method = 'credit'
          AND c.status = 'settled' AND c.settled_amount_usdc > 0
          AND c.updated_at >= $1 AND c.updated_at < $2
          AND NOT EXISTS (
              SELECT 1 FROM org_credit_ledger l
              WHERE l.org_id = c.org_id AND l.operation = 'debit' AND l.reason = 'run:' || c.invocation_id
          )) AS missing_debit,
       (array_agg(id ORDER BY id)
            FILTER (WHERE missing_charge OR status_mismatch OR amount_mismatch OR duplicate_debit))[1:5] AS sample_ids
FROM flagged
"""

# Direct MCP debits carry only 'mcp:' || tool, so they reconcile per (org, tool) rather than per call.
_MCP_DEBITS_SQL = """
WITH ledger AS (
    SELECT org_id, substring(reason FROM 5) AS capability, COUNT(*) AS n, SUM(amount_usdc) AS total
    FROM org_credit_ledger
    WHERE operation = 'debit' AND starts_with(reason, 'mcp:')
      AND created_at >= $1 AND created_at < $2
    GROUP BY 1, 2
), charges AS (
    SELECT c.org_id, c.capability, COUNT(*) AS n, SUM(c.settled_amount_usdc) AS total
    FROM billing_charges c
    WHERE c.source IN ('mcp', 'mcp_v1') AND c.billing_method = 'credit'
      AND c.status = 'settled' AND c.settled_amount_usdc > 0
      AND c.created_at >= $1 AND c.created_at < $2
      AND NOT EXISTS (
          SELECT 1 FROM pending_settlements p WHERE p.charge_id = c.id AND p.status = 'settled'
      )
    GROUP BY 1, 2
), joined AS (
    SELECT COALESCE(l.org_id, ch.org_id) || ':' || COALESCE(l.capability, ch.capability) AS group_key,
           COALESCE(l.n, 0) AS ledger_n,
           l.n IS DISTINCT FROM ch.n OR l.total IS DISTINCT FROM ch.total AS mismatch
    FROM ledger l
    FULL OUTER JOIN charges ch ON ch.org_id = l.org_id AND ch.capability = l.capability
)
SELECT COALESCE(SUM(ledger_n), 0) AS source_rows,
       COUNT(*) FILTER (WHERE mismatch) AS group_mismatch,
       (array_agg(group_key ORDER BY group_key) FILTER (WHERE mismatch))[1:5] AS sample_ids
FROM joined
"""

_REVENUE_SQL = """
SELECT (SELECT COALESCE(SUM(cost_usdc), 0)
        FROM usage_events
        WHERE settlement_status = 'settled' AND created_at >= $1 AND created_at < $2) AS legacy_revenue_usdc,
       COALESCE(SUM(settled_amount_usdc) FILTER (WHERE status = 'settled'), 0) AS ledger_revenue_usdc,
       COALESCE(SUM(settled_amount_usdc) FILTER (WHERE status = 'settled' AND source IN ('mcp', 'mcp_v1')), 0)
           AS ledger_mcp_revenue_usdc
FROM billing_charges
WHERE created_at >= $1 AND created_at < $2
"""


def _check(name: str, row: Any, discrepancy_keys: tuple[str, ...], info_keys: tuple[str, ...] = ()) -> dict:
    return {
        "name": name,
        "source_rows": int(row["source_rows"] or 0),
        "discrepancies": {key: int(row[key] or 0) for key in discrepancy_keys},
        "info": {key: int(row[key] or 0) for key in info_keys},
        "sample_ids": [str(value) for value in (row["sample_ids"] or [])],
    }


async def get_charge_reconciliation(start: datetime, end: datetime) -> dict:
    """Compare ``billing_charges`` with legacy billing records over ``[start, end)``."""
    if start >= end:
        raise ValueError("start must be before end")
    if end - start > MAX_WINDOW:
        raise ValueError("reconciliation window must not exceed 31 days")

    pool = _get_pool()
    runs = await pool.fetchrow(_AGENT_RUNS_SQL, start, end)
    calls = await pool.fetchrow(_MCP_CALLS_SQL, start, end)
    run_debits = await pool.fetchrow(_RUN_DEBITS_SQL, start, end)
    mcp_debits = await pool.fetchrow(_MCP_DEBITS_SQL, start, end)
    revenue = await pool.fetchrow(_REVENUE_SQL, start, end)

    checks = [
        _check(
            "agent_runs",
            runs,
            ("missing_charge", "status_mismatch", "amount_mismatch", "tx_mismatch", "unmatched_charge"),
        ),
        _check(
            "mcp_calls",
            calls,
            ("missing_charge", "status_mismatch", "amount_mismatch", "tx_mismatch", "unmatched_charge"),
            ("retry_settled",),
        ),
        _check(
            "credit_run_debits",
            run_debits,
            ("missing_charge", "status_mismatch", "amount_mismatch", "duplicate_debit", "missing_debit"),
        ),
        _check("credit_mcp_debits", mcp_debits, ("group_mismatch",)),
    ]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "ok": all(count == 0 for check in checks for count in check["discrepancies"].values()),
        "checks": checks,
        "legacy_revenue_usdc": int(revenue["legacy_revenue_usdc"] or 0),
        "ledger_revenue_usdc": int(revenue["ledger_revenue_usdc"] or 0),
        "ledger_mcp_revenue_usdc": int(revenue["ledger_mcp_revenue_usdc"] or 0),
    }
