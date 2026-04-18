"""Integration tests for BYOK platform fee billing flow.

Verifies that BYOK orgs are debited the flat platform fee (not LLM cost),
and that the platform_fee_usdc column is persisted in usage_events.
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
    get_credit_balance,
    get_invoice_by_run,
    get_invoices,
)
from usage import UsageEvent, record_usage_event
from users import create_user


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
        await conn.execute(
            """
            TRUNCATE TABLE org_credits, usage_events, siwe_nonces,
                           wallets, users, orgs RESTART IDENTITY CASCADE
            """
        )
        await conn.execute(
            """
            INSERT INTO pricing_rules (id, name, run_price_usdc)
            VALUES ('default', 'Default pricing', 10000)
            ON CONFLICT (id) DO NOTHING
            """
        )

    await pool.close()


async def _setup_org_with_credits(billing_db_pool, credit_amount: int = 100_000):
    """Create a test org with prepaid credits and return (org_id, user_id)."""
    user = await create_user("byok-test@example.com", "byok-test-user")
    user_id = user["id"]
    org_id = user["org_id"]
    await admin_topup_credit(org_id, credit_amount)
    return org_id, user_id


class TestByokPlatformFeeColumn:
    """Verify the platform_fee_usdc column round-trips through the DB."""

    @pytest.mark.asyncio
    async def test_usage_event_stores_platform_fee(self, billing_db_pool):
        org_id, user_id = await _setup_org_with_credits(billing_db_pool)

        event = UsageEvent(
            user_id=user_id,
            org_id=org_id,
            thread_id="t1",
            run_id="run-byok-1",
            tokens_in=100,
            tokens_out=50,
            cost_usdc=5000,
            platform_fee_usdc=1000,
        )
        await record_usage_event(event)

        invoices = await get_invoices(user_id, limit=1)
        assert len(invoices) == 1
        assert invoices[0]["platform_fee_usdc"] == 1000
        assert invoices[0]["cost_usdc"] == 5000

    @pytest.mark.asyncio
    async def test_usage_event_default_zero_platform_fee(self, billing_db_pool):
        org_id, user_id = await _setup_org_with_credits(billing_db_pool)

        event = UsageEvent(
            user_id=user_id,
            org_id=org_id,
            thread_id="t1",
            run_id="run-non-byok-1",
            cost_usdc=10_000,
        )
        await record_usage_event(event)

        invoice = await get_invoice_by_run("run-non-byok-1", user_id)
        assert invoice is not None
        assert invoice["platform_fee_usdc"] == 0


class TestByokDebitFlow:
    """Verify that BYOK orgs are debited platform_fee only, not cost_usdc."""

    @pytest.mark.asyncio
    async def test_byok_debit_is_platform_fee_only(self, billing_db_pool):
        org_id, user_id = await _setup_org_with_credits(billing_db_pool, 100_000)

        platform_fee = 1000
        # cost_usdc would be 50_000 but is informational only for BYOK

        # Debit only the platform fee (mirrors app.py BYOK settlement path)
        success = await debit_credit(org_id, platform_fee, reason="run:byok-test")
        assert success

        balance = await get_credit_balance(org_id)
        assert balance == 100_000 - platform_fee  # not cost_usdc

    @pytest.mark.asyncio
    async def test_non_byok_debit_is_full_cost(self, billing_db_pool):
        org_id, user_id = await _setup_org_with_credits(billing_db_pool, 100_000)

        cost_usdc = 10_000

        success = await debit_credit(org_id, cost_usdc, reason="run:non-byok-test")
        assert success

        balance = await get_credit_balance(org_id)
        assert balance == 100_000 - cost_usdc
