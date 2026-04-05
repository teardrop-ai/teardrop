"""Integration tests for billing.py — credit system, settlement recording,
billing history, and revenue summary against a real Postgres database.

Relies on the docker_postgres session fixture from tests/integration/conftest.py.
All migrations are applied via apply_pending() so pricing_rules, org_credits, and
other billing tables are available.
"""

from __future__ import annotations

import asyncpg
import pytest

import billing as billing_module
import usage as usage_module
import users as user_module
from billing import (
    admin_topup_credit,
    debit_credit,
    get_billing_history,
    get_credit_balance,
    get_invoice_by_run,
    get_invoices,
    get_revenue_summary,
    record_settlement,
    verify_credit,
)
from usage import UsageEvent, record_usage_event
from users import create_org, create_user


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def billing_db_pool(docker_postgres: str):
    """Pool with all migrations applied; wires billing/user/usage module pools."""
    from migrations.runner import apply_pending

    pool = await asyncpg.create_pool(docker_postgres, min_size=1, max_size=5)
    await apply_pending(pool)

    billing_module._pool = pool
    user_module._pool = pool
    usage_module._pool = pool

    yield pool

    async with pool.acquire() as conn:
        # Truncate in dependency order
        await conn.execute(
            """
            TRUNCATE TABLE org_credits, usage_events, siwe_nonces,
                           wallets, users, orgs RESTART IDENTITY CASCADE
            """
        )
        # Restore the default seed pricing rule
        await conn.execute(
            """
            INSERT INTO pricing_rules (id, name, run_price_usdc)
            VALUES ('default', 'Default pricing', 10000)
            ON CONFLICT (id) DO NOTHING
            """
        )

    billing_module._pool = None
    user_module._pool = None
    usage_module._pool = None
    await pool.close()


def _make_event(user_id: str, org_id: str, run_id: str, **overrides) -> UsageEvent:
    defaults = dict(
        user_id=user_id,
        org_id=org_id,
        thread_id="t1",
        run_id=run_id,
        tokens_in=100,
        tokens_out=50,
        tool_calls=0,
        tool_names=[],
        duration_ms=200,
        cost_usdc=0,
    )
    defaults.update(overrides)
    return UsageEvent(**defaults)


# ─── Credit balance ───────────────────────────────────────────────────────────


async def test_get_credit_balance_no_row(billing_db_pool):
    """Returns 0 when org has no credit row."""
    balance = await get_credit_balance("nonexistent-org-id")
    assert balance == 0


async def test_admin_topup_credit_creates_row(billing_db_pool):
    """First topup creates a new org_credits row."""
    org = await create_org("topup-org-1")
    new_balance = await admin_topup_credit(org.id, 100_000)
    assert new_balance == 100_000


async def test_admin_topup_credit_increments_existing(billing_db_pool):
    """Subsequent topups add to the existing balance (upsert behaviour)."""
    org = await create_org("topup-org-2")
    await admin_topup_credit(org.id, 100_000)
    new_balance = await admin_topup_credit(org.id, 50_000)
    assert new_balance == 150_000


async def test_get_credit_balance_reflects_topup(billing_db_pool):
    org = await create_org("balance-org")
    await admin_topup_credit(org.id, 200_000)
    balance = await get_credit_balance(org.id)
    assert balance == 200_000


# ─── verify_credit ────────────────────────────────────────────────────────────


async def test_verify_credit_sufficient(billing_db_pool):
    org = await create_org("verify-sufficient-org")
    await admin_topup_credit(org.id, 50_000)
    result = await verify_credit(org.id, 10_000)
    assert result.verified is True
    assert result.billing_method == "credit"


async def test_verify_credit_insufficient(billing_db_pool):
    org = await create_org("verify-insufficient-org")
    await admin_topup_credit(org.id, 5_000)
    result = await verify_credit(org.id, 10_000)
    assert result.verified is False
    assert "Insufficient credit" in result.error


async def test_verify_credit_exact_balance_passes(billing_db_pool):
    org = await create_org("verify-exact-org")
    await admin_topup_credit(org.id, 10_000)
    result = await verify_credit(org.id, 10_000)
    assert result.verified is True


# ─── debit_credit ─────────────────────────────────────────────────────────────


async def test_debit_credit_decreases_balance(billing_db_pool):
    org = await create_org("debit-org-1")
    await admin_topup_credit(org.id, 100_000)
    success = await debit_credit(org.id, 30_000)
    assert success is True
    balance = await get_credit_balance(org.id)
    assert balance == 70_000


async def test_debit_credit_floors_at_zero(billing_db_pool):
    """Debiting more than the balance floors at 0 — never goes negative."""
    org = await create_org("floor-org")
    await admin_topup_credit(org.id, 10_000)
    success = await debit_credit(org.id, 999_999)
    assert success is True
    balance = await get_credit_balance(org.id)
    assert balance == 0


async def test_debit_credit_no_row_returns_false(billing_db_pool):
    """If no org_credits row exists, debit returns False without error."""
    org = await create_org("no-credit-org")
    # Do NOT call admin_topup_credit — no row in org_credits
    success = await debit_credit(org.id, 10_000)
    assert success is False


async def test_debit_credit_full_balance(billing_db_pool):
    """Debiting exactly the balance leaves it at 0."""
    org = await create_org("full-debit-org")
    await admin_topup_credit(org.id, 50_000)
    success = await debit_credit(org.id, 50_000)
    assert success is True
    assert await get_credit_balance(org.id) == 0


# ─── record_settlement ────────────────────────────────────────────────────────


async def test_record_settlement_updates_usage_event(billing_db_pool):
    """record_settlement writes cost, tx_hash, and status to usage_events."""
    org = await create_org("settle-org")
    user = await create_user("settle@test.com", "password1234", org.id, "user")
    event = _make_event(user.id, org.id, "run-settle-1")
    await record_usage_event(event)

    await record_settlement(event.id, 10_000, "0xabc123", "settled")

    row = await billing_db_pool.fetchrow(
        "SELECT cost_usdc, settlement_tx, settlement_status FROM usage_events WHERE id = $1",
        event.id,
    )
    assert row is not None
    assert int(row["cost_usdc"]) == 10_000
    assert row["settlement_tx"] == "0xabc123"
    assert row["settlement_status"] == "settled"


async def test_record_settlement_failed_status(billing_db_pool):
    """Settlement records 'failed' status when settlement doesn't complete."""
    org = await create_org("fail-settle-org")
    user = await create_user("fail-settle@test.com", "password1234", org.id, "user")
    event = _make_event(user.id, org.id, "run-settle-fail")
    await record_usage_event(event)

    await record_settlement(event.id, 0, "", "failed")

    row = await billing_db_pool.fetchrow(
        "SELECT settlement_status FROM usage_events WHERE id = $1",
        event.id,
    )
    assert row["settlement_status"] == "failed"


# ─── get_billing_history ──────────────────────────────────────────────────────


async def test_get_billing_history_empty(billing_db_pool):
    result = await get_billing_history("nonexistent-user-id")
    assert result == []


async def test_get_billing_history_returns_only_settled(billing_db_pool):
    """Billing history excludes events with settlement_status='none'."""
    org = await create_org("history-org")
    user = await create_user("history@test.com", "password1234", org.id, "user")

    unsettled = _make_event(user.id, org.id, "run-unsettled")
    await record_usage_event(unsettled)

    settled = _make_event(user.id, org.id, "run-settled", cost_usdc=5_000)
    await record_usage_event(settled)
    await record_settlement(settled.id, 5_000, "0xtx1", "settled")

    history = await get_billing_history(user.id)
    assert len(history) == 1
    assert history[0]["run_id"] == "run-settled"


async def test_get_billing_history_respects_limit(billing_db_pool):
    """The limit parameter caps the number of returned rows."""
    org = await create_org("limit-org")
    user = await create_user("limit@test.com", "password1234", org.id, "user")
    for i in range(5):
        ev = _make_event(user.id, org.id, f"run-lim-{i}", cost_usdc=1_000)
        await record_usage_event(ev)
        await record_settlement(ev.id, 1_000, f"0x{i}", "settled")

    result = await get_billing_history(user.id, limit=3)
    assert len(result) == 3


# ─── get_invoices ─────────────────────────────────────────────────────────────


async def test_get_invoices_includes_all_events(billing_db_pool):
    """get_invoices returns all events, not just settled ones."""
    org = await create_org("invoice-org")
    user = await create_user("invoice@test.com", "password1234", org.id, "user")
    for i in range(3):
        ev = _make_event(user.id, org.id, f"run-inv-{i}")
        await record_usage_event(ev)

    invoices = await get_invoices(user.id)
    assert len(invoices) == 3


async def test_get_invoices_scoped_to_user(billing_db_pool):
    """get_invoices does not return events from other users."""
    org = await create_org("inv-scope-org")
    user_a = await create_user("inv-a@test.com", "password1234", org.id, "user")
    user_b = await create_user("inv-b@test.com", "password1234", org.id, "user")
    for i in range(2):
        ev = _make_event(user_a.id, org.id, f"run-a-{i}")
        await record_usage_event(ev)
    ev_b = _make_event(user_b.id, org.id, "run-b-0")
    await record_usage_event(ev_b)

    invoices_a = await get_invoices(user_a.id)
    assert len(invoices_a) == 2
    assert all(r["run_id"].startswith("run-a-") for r in invoices_a)


# ─── get_invoice_by_run ───────────────────────────────────────────────────────


async def test_get_invoice_by_run_found(billing_db_pool):
    org = await create_org("inv-run-org")
    user = await create_user("inv-run@test.com", "password1234", org.id, "user")
    ev = _make_event(user.id, org.id, "run-specific")
    await record_usage_event(ev)

    result = await get_invoice_by_run("run-specific", user.id)
    assert result is not None
    assert result["run_id"] == "run-specific"


async def test_get_invoice_by_run_wrong_user_returns_none(billing_db_pool):
    """Security: cannot retrieve another user's invoice by guessing run_id."""
    org = await create_org("inv-sec-org")
    user_a = await create_user("inv-sec-a@test.com", "password1234", org.id, "user")
    ev = _make_event(user_a.id, org.id, "run-user-a")
    await record_usage_event(ev)

    result = await get_invoice_by_run("run-user-a", "attacker-user-id")
    assert result is None


async def test_get_invoice_by_run_nonexistent_returns_none(billing_db_pool):
    result = await get_invoice_by_run("run-does-not-exist", "any-user")
    assert result is None


# ─── get_revenue_summary ──────────────────────────────────────────────────────


async def test_get_revenue_summary_empty(billing_db_pool):
    summary = await get_revenue_summary()
    assert summary["total_settlements"] == 0
    assert summary["total_revenue_usdc"] == 0


async def test_get_revenue_summary_aggregates_settled_events(billing_db_pool):
    org = await create_org("rev-org")
    user = await create_user("rev@test.com", "password1234", org.id, "user")
    costs = [10_000, 20_000, 5_000]
    for i, cost in enumerate(costs):
        ev = _make_event(user.id, org.id, f"run-rev-{i}", cost_usdc=cost)
        await record_usage_event(ev)
        await record_settlement(ev.id, cost, f"0x{i}", "settled")

    summary = await get_revenue_summary()
    assert summary["total_settlements"] == 3
    assert summary["total_revenue_usdc"] == 35_000


async def test_get_revenue_summary_excludes_failed_settlements(billing_db_pool):
    """Failed settlements should not count toward revenue."""
    org = await create_org("rev-fail-org")
    user = await create_user("rev-fail@test.com", "password1234", org.id, "user")
    ev = _make_event(user.id, org.id, "run-rev-fail", cost_usdc=10_000)
    await record_usage_event(ev)
    await record_settlement(ev.id, 0, "", "failed")

    summary = await get_revenue_summary()
    assert summary["total_settlements"] == 0
    assert summary["total_revenue_usdc"] == 0
