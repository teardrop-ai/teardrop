# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for the unified billing charge ledger helpers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import billing
import billing.charges as charges
import billing.settlement as settlement


def test_charge_id_is_deterministic_per_source_and_invocation():
    assert charges.charge_id_for("mcp", "call-1") == charges.charge_id_for("mcp", "call-1")
    assert charges.charge_id_for("mcp", "call-1") != charges.charge_id_for("mcp_v1", "call-1")
    assert charges.charge_id_for("api", "run-1") != charges.charge_id_for("api", "run-2")


@pytest.mark.anyio
async def test_record_charge_is_idempotent_and_sanitized():
    pool = MagicMock()
    pool.execute = AsyncMock()

    with (
        patch.object(charges, "_has_pool", return_value=True),
        patch.object(charges, "_get_pool", return_value=pool),
    ):
        charge_id = await charges.record_charge(
            source="mcp",
            invocation_id="call-1",
            org_id="",
            payer_address=" 0xABCdef ",
            capability="platform/get_price",
            billing_method="x402",
            amount_usdc=2_000,
            status="settled",
            settled_amount_usdc=2_000,
            settlement_tx="0xtx",
        )

    assert charge_id == charges.charge_id_for("mcp", "call-1")
    sql, *args = pool.execute.await_args.args
    assert "ON CONFLICT (source, invocation_id) DO NOTHING" in sql
    assert args == [
        charge_id,
        "mcp",
        "call-1",
        "",
        "",
        "",
        "0xabcdef",
        "platform/get_price",
        "x402",
        2_000,
        2_000,
        "settled",
        "0xtx",
    ]


@pytest.mark.anyio
async def test_failed_charge_never_records_settled_amount_or_tx():
    pool = MagicMock()
    pool.execute = AsyncMock()

    with (
        patch.object(charges, "_has_pool", return_value=True),
        patch.object(charges, "_get_pool", return_value=pool),
    ):
        await charges.record_charge(
            source="api",
            invocation_id="run-1",
            billing_method="credit",
            amount_usdc=5_000,
            status="failed",
            settled_amount_usdc=5_000,
            settlement_tx="0xignored",
        )

    args = pool.execute.await_args.args[1:]
    assert args[10] == 0
    assert args[11] == "failed"
    assert args[12] == ""


@pytest.mark.anyio
async def test_record_charge_never_raises_and_skips_without_pool():
    pool = MagicMock()
    pool.execute = AsyncMock(side_effect=RuntimeError("db down"))

    with (
        patch.object(charges, "_has_pool", return_value=True),
        patch.object(charges, "_get_pool", return_value=pool),
    ):
        charge_id = await charges.record_charge(
            source="api", invocation_id="run-1", billing_method="credit", amount_usdc=1, status="settled"
        )
    assert charge_id == charges.charge_id_for("api", "run-1")

    with patch.object(charges, "_has_pool", return_value=False):
        assert await charges.record_charge(
            source="api", invocation_id="run-2", billing_method="credit", amount_usdc=1, status="settled"
        ) == charges.charge_id_for("api", "run-2")


@pytest.mark.anyio
async def test_status_transitions_are_guarded():
    conn = MagicMock()
    conn.execute = AsyncMock()
    await charges.mark_charge_settled(conn, "charge-1", 700)
    sql, charge_id, amount = conn.execute.await_args.args
    assert "WHERE id = $1 AND status = 'failed'" in sql
    assert (charge_id, amount) == ("charge-1", 700)

    pool = MagicMock()
    pool.execute = AsyncMock()
    with (
        patch.object(charges, "_has_pool", return_value=True),
        patch.object(charges, "_get_pool", return_value=pool),
    ):
        await charges.mark_charge_reverted("ue-1", "0xtx")
        await charges.mark_charge_reverted("ue-1", "")
    pool.execute.assert_awaited_once()
    assert "status = 'settled'" in pool.execute.await_args.args[0]


@pytest.mark.anyio
async def test_root_enqueue_wrapper_forwards_principal_and_charge():
    """Regression: the root wrapper rejected principal_id, so agent-run credit retries raised TypeError."""
    with patch.object(billing, "_ENQUEUE_FAILED_SETTLEMENT_ORIG", AsyncMock()) as enqueue:
        await billing.enqueue_failed_settlement(
            "ue-1", "org-1", "run-1", "credit", 500, principal_id="user-1", charge_id="charge-1"
        )

    assert enqueue.await_args.kwargs == {
        "principal_id": "user-1",
        "charge_id": "charge-1",
        "earnings_allocations": None,
    }


@pytest.mark.anyio
async def test_enqueue_persists_charge_link():
    pool = MagicMock()
    pool.execute = AsyncMock()

    with patch.object(settlement, "_get_pool", return_value=pool):
        await settlement.enqueue_failed_settlement("ue-1", "org-1", "run-1", "credit", 500, charge_id="charge-1")

    sql, *args = pool.execute.await_args.args
    assert "charge_id" in sql
    assert args[-2] == "charge-1"
    assert args[-1] == "[]"


_ALLOCATION = {
    "author_org_id": "author-org",
    "caller_org_id": "org-1",
    "tool_name": "weather",
    "total_cost_usdc": 1_000,
}


@pytest.mark.anyio
async def test_enqueue_persists_earnings_allocations():
    pool = MagicMock()
    pool.execute = AsyncMock()

    with patch.object(settlement, "_get_pool", return_value=pool):
        await settlement.enqueue_failed_settlement(
            "ue-1", "org-1", "run-1", "credit", 500, charge_id="charge-1", earnings_allocations=[_ALLOCATION]
        )

    args = pool.execute.await_args.args[1:]
    assert json.loads(args[-1]) == [_ALLOCATION]


def _claimed_credit_harness(debit_success: bool):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="ps-1")
    conn.execute = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    service = MagicMock()
    service._debit_credit_locked = AsyncMock(return_value=(debit_success, 500 if debit_success else 0, None))
    row = {
        "id": "ps-1",
        "org_id": "org-1",
        "run_id": "run-1",
        "usage_event_id": "ue-1",
        "amount_usdc": 500,
        "principal_id": None,
        "charge_id": "",
        "next_retry_at": "lease",
        "earnings_allocations": json.dumps([_ALLOCATION]),
    }
    return pool, conn, service, row


@pytest.mark.anyio
@pytest.mark.parametrize("debit_success", [True, False])
async def test_credit_retry_pays_authors_in_debit_transaction(debit_success):
    """Recovered credit charges insert stored author earnings on the claim's connection; failures insert none."""
    pool, conn, service, row = _claimed_credit_harness(debit_success)
    insert = AsyncMock(return_value=True)
    cache = MagicMock()
    cache.invalidate = AsyncMock()

    with (
        patch.object(settlement, "_get_credit_service", return_value=service),
        patch.object(settlement, "_get_daily_spend_cache", return_value=cache),
        patch("marketplace.insert_tool_call_earnings", insert),
    ):
        success, amount = await settlement._settle_claimed_credit(pool, row, 1)

    assert (success, amount) == ((True, 500) if debit_success else (False, 0))
    if debit_success:
        insert.assert_awaited_once_with(conn, "author-org", "weather", "org-1", 1_000)
    else:
        insert.assert_not_awaited()


@pytest.mark.anyio
async def test_credit_retry_skips_constraint_rejected_allocation_but_collects_charge():
    """A permanently invalid allocation (e.g. deleted author org) never blocks recovering the charge."""
    from psycopg.errors import ForeignKeyViolation

    pool, _conn, service, row = _claimed_credit_harness(True)
    cache = MagicMock()
    cache.invalidate = AsyncMock()
    with (
        patch.object(settlement, "_get_credit_service", return_value=service),
        patch.object(settlement, "_get_daily_spend_cache", return_value=cache),
        patch("marketplace.insert_tool_call_earnings", AsyncMock(side_effect=ForeignKeyViolation())),
    ):
        assert await settlement._settle_claimed_credit(pool, row, 1) == (True, 500)


@pytest.mark.anyio
async def test_credit_retry_transient_earnings_failure_rolls_back_for_retry():
    """Transient SQL errors propagate so the debit rolls back and the worker retries both together."""
    from psycopg import OperationalError

    pool, _conn, service, row = _claimed_credit_harness(True)
    with (
        patch.object(settlement, "_get_credit_service", return_value=service),
        patch("marketplace.insert_tool_call_earnings", AsyncMock(side_effect=OperationalError("statement timeout"))),
        pytest.raises(OperationalError),
    ):
        await settlement._settle_claimed_credit(pool, row, 1)
