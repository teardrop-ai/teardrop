# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for the billing_charges reconciliation report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import billing.reconciliation as reconciliation

_END = datetime(2026, 9, 29, tzinfo=timezone.utc)
_START = _END - timedelta(days=1)

_CLEAN_ROWS = [
    {
        "source_rows": 3,
        "missing_charge": 0,
        "status_mismatch": 0,
        "amount_mismatch": 0,
        "tx_mismatch": 0,
        "unmatched_charge": 0,
        "sample_ids": None,
    },
    {
        "source_rows": 2,
        "missing_charge": 0,
        "status_mismatch": 0,
        "amount_mismatch": 0,
        "tx_mismatch": 0,
        "unmatched_charge": 0,
        "retry_settled": 1,
        "sample_ids": None,
    },
    {
        "source_rows": 2,
        "missing_charge": 0,
        "status_mismatch": 0,
        "amount_mismatch": 0,
        "duplicate_debit": 0,
        "missing_debit": 0,
        "sample_ids": None,
    },
    {"source_rows": 1, "group_mismatch": 0, "sample_ids": None},
    {"legacy_revenue_usdc": 25_000, "ledger_revenue_usdc": 32_000, "ledger_mcp_revenue_usdc": 7_000},
]


async def _run(rows: list[dict]) -> dict:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=rows)
    with patch.object(reconciliation, "_get_pool", return_value=pool):
        return await reconciliation.get_charge_reconciliation(_START, _END)


@pytest.mark.anyio
async def test_clean_ledger_reports_ok_with_revenue_comparison():
    report = await _run([dict(row) for row in _CLEAN_ROWS])

    assert report["ok"] is True
    assert [check["name"] for check in report["checks"]] == [
        "agent_runs",
        "mcp_calls",
        "credit_run_debits",
        "credit_mcp_debits",
    ]
    assert report["checks"][1]["info"] == {"retry_settled": 1}
    assert (report["legacy_revenue_usdc"], report["ledger_revenue_usdc"], report["ledger_mcp_revenue_usdc"]) == (
        25_000,
        32_000,
        7_000,
    )
    assert report["start"] == _START.isoformat()


@pytest.mark.anyio
async def test_any_discrepancy_fails_the_gate():
    rows = [dict(row) for row in _CLEAN_ROWS]
    rows[2] = {**rows[2], "missing_debit": 1, "sample_ids": ["ledger-1"]}

    report = await _run(rows)

    assert report["ok"] is False
    assert report["checks"][2]["discrepancies"]["missing_debit"] == 1
    assert report["checks"][2]["sample_ids"] == ["ledger-1"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("start", "end"),
    [(_END, _END), (_END, _START), (_END - timedelta(days=32), _END)],
)
async def test_invalid_windows_are_rejected_before_querying(start, end):
    with patch.object(reconciliation, "_get_pool") as get_pool:
        with pytest.raises(ValueError):
            await reconciliation.get_charge_reconciliation(start, end)
    get_pool.assert_not_called()


def test_sql_is_safe_for_psycopg_placeholder_translation():
    statements = [
        reconciliation._AGENT_RUNS_SQL,
        reconciliation._MCP_CALLS_SQL,
        reconciliation._RUN_DEBITS_SQL,
        reconciliation._MCP_DEBITS_SQL,
        reconciliation._REVENUE_SQL,
    ]
    for sql in statements:
        assert "%" not in sql


def test_retried_mcp_charges_reconcile_against_run_debits_not_mcp_groups():
    assert "p.status = 'settled'" in reconciliation._MCP_DEBITS_SQL
    assert "NOT EXISTS" in reconciliation._MCP_DEBITS_SQL
    assert "'mcp', 'mcp_v1'" in reconciliation._RUN_DEBITS_SQL
    assert "starts_with(l.reason, 'run:')" in reconciliation._RUN_DEBITS_SQL
    assert "a2a_delegation" not in reconciliation._RUN_DEBITS_SQL + reconciliation._MCP_DEBITS_SQL
