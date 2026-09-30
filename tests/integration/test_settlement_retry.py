# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Integration tests for the settlement retry lease against a real Postgres.

The retry claim, the credit debit, and every follow-up status write are guarded by
a lease token (``next_retry_at``) that only Postgres produces. Mocked row shapes
cannot prove those guards work, so these tests exercise the real SQL end to end.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

import billing as billing_module
import teardrop.usage as usage_module
import teardrop.users as user_module
from billing import admin_topup_credit, get_credit_balance
from billing.context import _bind_pool, _clear_pool
from billing.settlement import SettlementClaimLostError, process_pending_settlements, rearm_exhausted_credit_settlements
from shared.db_pool import create_pool
from teardrop.usage import UsageEvent, record_usage_event
from teardrop.users import create_org


@pytest.fixture
async def settlement_pool(docker_postgres: str):
    from migrations.runner import apply_pending

    pool = await create_pool(docker_postgres, min_size=1, max_size=5, name="integration-settlement")
    await apply_pending(pool)

    _bind_pool(pool)
    billing_module._pool = pool
    user_module.base._pool = pool
    usage_module._pool = pool

    yield pool

    async with pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE TABLE pending_settlements, billing_charges, mcp_call_events, org_credit_ledger, org_credits,
                           usage_events, siwe_nonces, wallets, users, orgs
            RESTART IDENTITY CASCADE
            """
        )

    _clear_pool()
    billing_module._pool = None
    user_module.base._pool = None
    usage_module._pool = None
    await pool.close()


async def _seed(pool, *, balance: int, amount: int, billing_method: str = "credit"):
    """Create a funded org with one usage event and one due pending settlement."""
    org = await create_org(f"settle-org-{uuid.uuid4().hex[:8]}")
    if balance:
        await admin_topup_credit(org.id, balance)

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    event = UsageEvent(
        user_id="",
        org_id=org.id,
        thread_id="t1",
        run_id=run_id,
        tokens_in=10,
        tokens_out=5,
        tool_calls=0,
        tool_names=[],
        duration_ms=10,
        cost_usdc=amount,
    )
    await record_usage_event(event)

    settlement_id = str(uuid.uuid4())
    await pool.execute(
        """
        INSERT INTO pending_settlements
            (id, usage_event_id, org_id, run_id, billing_method,
             amount_usdc, retry_count, max_retries, next_retry_at, status)
        VALUES ($1, $2, $3, $4, $5, $6, 0, 3, NOW() - INTERVAL '1 second', 'pending')
        """,
        settlement_id,
        event.id,
        org.id,
        run_id,
        billing_method,
        amount,
    )
    return org, event, settlement_id


async def _settlement(pool, settlement_id: str):
    return await pool.fetchrow("SELECT * FROM pending_settlements WHERE id = $1", settlement_id)


async def test_due_credit_settlement_is_leased_debited_and_finalized(settlement_pool):
    """The happy path must actually debit and reach a terminal 'settled' state."""
    org, event, settlement_id = await _seed(settlement_pool, balance=100_000, amount=25_000)

    assert await process_pending_settlements() == 1

    assert await get_credit_balance(org.id) == 75_000

    row = await _settlement(settlement_pool, settlement_id)
    assert row["status"] == "settled"
    assert row["retry_count"] == 1
    assert row["last_error"] == ""

    usage = await settlement_pool.fetchrow("SELECT cost_usdc, settlement_status FROM usage_events WHERE id = $1", event.id)
    assert usage["settlement_status"] == "settled"
    assert usage["cost_usdc"] == 25_000

    ledger = await settlement_pool.fetch(
        "SELECT amount_usdc, operation FROM org_credit_ledger WHERE org_id = $1 AND operation = 'debit'",
        org.id,
    )
    assert [r["amount_usdc"] for r in ledger] == [25_000]


async def test_settled_row_is_never_debited_twice(settlement_pool):
    """A second worker pass must not re-claim or re-debit a settled row."""
    org, _event, settlement_id = await _seed(settlement_pool, balance=100_000, amount=25_000)

    assert await process_pending_settlements() == 1
    assert await process_pending_settlements() == 0

    assert await get_credit_balance(org.id) == 75_000
    ledger_count = await settlement_pool.fetchval(
        "SELECT COUNT(*) FROM org_credit_ledger WHERE org_id = $1 AND operation = 'debit'",
        org.id,
    )
    assert ledger_count == 1


async def test_stale_lease_holder_aborts_without_touching_the_row(settlement_pool):
    """A worker whose lease was stolen must not debit or clobber the new holder."""
    from billing.settlement import _settle_claimed_credit

    org, event, settlement_id = await _seed(settlement_pool, balance=100_000, amount=25_000)

    # Worker B holds the live lease.
    live_lease = await settlement_pool.fetchval(
        """
        UPDATE pending_settlements
        SET status = 'retrying', next_retry_at = NOW() + INTERVAL '5 minutes'
        WHERE id = $1
        RETURNING next_retry_at
        """,
        settlement_id,
    )

    # Worker A still believes it holds an earlier, now-superseded lease.
    stale_row = {
        "id": settlement_id,
        "usage_event_id": event.id,
        "org_id": org.id,
        "run_id": "run-stale",
        "amount_usdc": 25_000,
        "principal_id": None,
        "next_retry_at": live_lease - timedelta(minutes=1),
    }

    with pytest.raises(SettlementClaimLostError):
        await _settle_claimed_credit(settlement_pool, stale_row, 1)

    assert await get_credit_balance(org.id) == 100_000
    row = await _settlement(settlement_pool, settlement_id)
    assert row["status"] == "retrying"
    assert row["next_retry_at"] == live_lease


async def test_insufficient_balance_backs_off_under_the_live_lease(settlement_pool):
    """A failed debit must persist the error and a future retry via the lease guard."""
    org, _event, settlement_id = await _seed(settlement_pool, balance=1_000, amount=25_000)

    assert await process_pending_settlements() == 0

    assert await get_credit_balance(org.id) == 1_000
    row = await _settlement(settlement_pool, settlement_id)
    assert row["status"] == "retrying"
    assert row["retry_count"] == 1
    assert row["last_error"] == "debit_credit returned False"


async def test_x402_row_reaches_exhausted_instead_of_looping(settlement_pool):
    """x402 rows are not retryable and must land in a terminal 'exhausted' state."""
    _org, _event, settlement_id = await _seed(settlement_pool, balance=0, amount=25_000, billing_method="x402")

    assert await process_pending_settlements() == 0

    row = await _settlement(settlement_pool, settlement_id)
    assert row["status"] == "exhausted"
    assert "x402 settlements cannot be retried" in row["last_error"]


async def test_credit_retry_settles_linked_charge_and_ledger_rejects_rewrites(settlement_pool):
    from billing.charges import record_charge

    org, event, settlement_id = await _seed(settlement_pool, balance=100_000, amount=25_000)
    charge_kwargs = dict(
        source="api",
        invocation_id=event.run_id,
        usage_event_id=event.id,
        org_id=org.id,
        billing_method="credit",
        amount_usdc=25_000,
        status="failed",
    )
    charge_id = await record_charge(**charge_kwargs)
    await settlement_pool.execute("UPDATE pending_settlements SET charge_id = $2 WHERE id = $1", settlement_id, charge_id)

    assert await process_pending_settlements() == 1

    async def _charge():
        row = await settlement_pool.fetchrow("SELECT status, settled_amount_usdc FROM billing_charges WHERE id = $1", charge_id)
        return row["status"], row["settled_amount_usdc"]

    assert await _charge() == ("settled", 25_000)

    # A late duplicate of the original failed outcome must not clobber the settled charge.
    assert await record_charge(**charge_kwargs) == charge_id
    assert await _charge() == ("settled", 25_000)

    with pytest.raises(Exception, match="immutable"):
        await settlement_pool.execute("UPDATE billing_charges SET amount_usdc = 1 WHERE id = $1", charge_id)
    with pytest.raises(Exception, match="cannot move"):
        await settlement_pool.execute("UPDATE billing_charges SET status = 'failed' WHERE id = $1", charge_id)
    with pytest.raises(Exception, match="append-only"):
        await settlement_pool.execute("DELETE FROM billing_charges WHERE id = $1", charge_id)
    assert await _charge() == ("settled", 25_000)


async def test_charge_reconciliation_passes_when_consistent_and_flags_phantom_charge(settlement_pool):
    from datetime import datetime, timezone

    from billing import debit_credit, get_charge_reconciliation
    from billing.charges import record_charge
    from teardrop.usage import record_mcp_call_event

    window_start = datetime.now(timezone.utc) - timedelta(hours=1)

    # Agent run whose failed credit debit is settled by the retry worker.
    org, event, run_settlement_id = await _seed(settlement_pool, balance=100_000, amount=25_000)
    run_charge = await record_charge(
        source="api",
        invocation_id=event.run_id,
        usage_event_id=event.id,
        org_id=org.id,
        billing_method="credit",
        amount_usdc=25_000,
        status="failed",
    )
    await settlement_pool.execute("UPDATE pending_settlements SET charge_id = $2 WHERE id = $1", run_settlement_id, run_charge)

    # MCP call whose failed credit debit is retried under the call id.
    retried_call = str(uuid.uuid4())
    await record_mcp_call_event(retried_call, org.id, "", "acme/tool", "credit", 4_000, "failed")
    retried_charge = await record_charge(
        source="mcp",
        invocation_id=retried_call,
        org_id=org.id,
        capability="acme/tool",
        billing_method="credit",
        amount_usdc=4_000,
        status="failed",
    )
    await settlement_pool.execute(
        """
        INSERT INTO pending_settlements
            (id, usage_event_id, org_id, run_id, billing_method, amount_usdc, charge_id, next_retry_at)
        VALUES ($1, $2, $3, $2, 'credit', 4000, $4, NOW() - INTERVAL '1 second')
        """,
        str(uuid.uuid4()),
        retried_call,
        org.id,
        retried_charge,
    )
    assert await process_pending_settlements() == 2

    # Direct MCP credit call settled on the first attempt.
    direct_call = str(uuid.uuid4())
    assert (await debit_credit(org.id, 3_000, reason="mcp:acme/tool"))[0]
    await record_charge(
        source="mcp",
        invocation_id=direct_call,
        org_id=org.id,
        capability="acme/tool",
        billing_method="credit",
        amount_usdc=3_000,
        status="settled",
        settled_amount_usdc=3_000,
    )
    await record_mcp_call_event(direct_call, org.id, "", "acme/tool", "credit", 3_000, "settled")

    async def _report():
        report = await get_charge_reconciliation(window_start, datetime.now(timezone.utc) + timedelta(minutes=1))
        return report, {check["name"]: check for check in report["checks"]}

    report, checks = await _report()
    assert report["ok"] is True, report
    assert checks["agent_runs"]["source_rows"] == 1
    assert checks["mcp_calls"]["source_rows"] == 2
    assert checks["mcp_calls"]["info"]["retry_settled"] == 1
    assert checks["credit_run_debits"]["source_rows"] == 2
    assert checks["credit_mcp_debits"]["source_rows"] == 1
    assert report["legacy_revenue_usdc"] == 25_000
    assert (report["ledger_revenue_usdc"], report["ledger_mcp_revenue_usdc"]) == (32_000, 7_000)

    # A settled credit charge with no debit behind it is revenue that never moved.
    await record_charge(
        source="api",
        invocation_id="phantom-run",
        org_id=org.id,
        billing_method="credit",
        amount_usdc=1_000,
        status="settled",
        settled_amount_usdc=1_000,
    )

    report, checks = await _report()
    assert report["ok"] is False
    assert checks["credit_run_debits"]["discrepancies"]["missing_debit"] == 1
    assert checks["agent_runs"]["discrepancies"]["unmatched_charge"] == 1

    # Terminal means terminal: the claim query must not pick it up again.
    assert await process_pending_settlements() == 0


async def _insert_exhausted(pool, org_id: str, amount: int, *, billing_method: str = "credit", age: str = "1 hour"):
    settlement_id = str(uuid.uuid4())
    await pool.execute(
        """
        INSERT INTO pending_settlements
            (id, usage_event_id, org_id, run_id, billing_method,
             amount_usdc, retry_count, max_retries, next_retry_at, status, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, 3, 3, NOW(), 'exhausted', NOW() - $7::INTERVAL)
        """,
        settlement_id,
        str(uuid.uuid4()),
        org_id,
        f"run-{uuid.uuid4().hex[:8]}",
        billing_method,
        amount,
        age,
    )
    return settlement_id


async def test_topup_rearm_is_fifo_capped_windowed_and_credit_only(settlement_pool):
    """Only recent credit debt that fits the top-up is re-queued, then debited exactly once."""
    org = await create_org(f"rearm-org-{uuid.uuid4().hex[:8]}")
    other_org = await create_org(f"rearm-other-{uuid.uuid4().hex[:8]}")
    await admin_topup_credit(org.id, 60_000)

    oldest = await _insert_exhausted(settlement_pool, org.id, 30_000, age="2 days")
    over_cap = await _insert_exhausted(settlement_pool, org.id, 50_000, age="1 day")
    x402_row = await _insert_exhausted(settlement_pool, org.id, 10_000, billing_method="x402")
    stale = await _insert_exhausted(settlement_pool, org.id, 5_000, age="8 days")
    foreign = await _insert_exhausted(settlement_pool, other_org.id, 1_000)

    assert await rearm_exhausted_credit_settlements(org.id, 60_000) == 1

    statuses = {sid: (await _settlement(settlement_pool, sid))["status"] for sid in (oldest, over_cap, x402_row, stale, foreign)}
    assert statuses == {
        oldest: "pending",
        over_cap: "exhausted",
        x402_row: "exhausted",
        stale: "exhausted",
        foreign: "exhausted",
    }
    assert (await _settlement(settlement_pool, oldest))["retry_count"] == 0

    assert await process_pending_settlements() == 1
    assert await process_pending_settlements() == 0
    assert await get_credit_balance(org.id) == 30_000
    assert (await _settlement(settlement_pool, oldest))["status"] == "settled"
