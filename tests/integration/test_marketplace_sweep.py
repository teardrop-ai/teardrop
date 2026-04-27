"""Integration tests for the marketplace auto-sweep worker (Phase 0.3).

Requires a live Postgres instance (Docker or DATABASE_URL env var).
CDP / agent_wallet calls are mocked so no real on-chain transfers occur.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

import marketplace as marketplace_module
import users as users_module
from marketplace import (
    marketplace_sweep_once,
    reset_withdrawal,
    set_author_config,
)
from users import create_org

_VALID_ADDR = "0x1234567890123456789012345678901234567890"


# ─── DB fixture ───────────────────────────────────────────────────────────────


@pytest.fixture
async def sweep_db_pool(docker_postgres: str):
    """Isolated DB pool with migrations applied for sweep integration tests."""
    from migrations.runner import apply_pending

    pool = await asyncpg.create_pool(docker_postgres, min_size=1, max_size=5)
    await apply_pending(pool)

    marketplace_module._pool = pool
    users_module._pool = pool

    yield pool

    async with pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE TABLE
                tool_author_earnings,
                tool_author_withdrawals,
                org_author_configs,
                users,
                orgs
            RESTART IDENTITY CASCADE
            """
        )

    marketplace_module._pool = None
    users_module._pool = None
    await pool.close()


# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _seed_org_with_earnings(pool: asyncpg.Pool, org_id: str, amount: int) -> None:
    """Insert a pending earnings row for the org."""
    await pool.execute(
        """
        INSERT INTO tool_author_earnings
            (id, org_id, tool_name, caller_org_id,
             total_cost_usdc, author_share_usdc, platform_share_usdc,
             status, created_at)
        VALUES (gen_random_uuid()::TEXT, $1, 'test_tool', 'caller-org',
                $2, $2, 0, 'pending', NOW())
        """,
        org_id,
        amount,
    )


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_sweep_end_to_end_mock_cdp(sweep_db_pool):
    """Full sweep cycle: creates withdrawal, settles it via mocked CDP, counts 1."""
    pool = sweep_db_pool

    org = await create_org("sweep-test-org")
    await set_author_config(org.id, settlement_wallet=_VALID_ADDR)
    await _seed_org_with_earnings(pool, org.id, 500_000)  # $0.50

    with (
        patch("marketplace.get_settings") as mock_settings,
        patch("agent_wallets.transfer_usdc", new=AsyncMock(return_value="0xtxhash")),
    ):
        settings = MagicMock()
        settings.marketplace_minimum_withdrawal_usdc = 100_000
        settings.marketplace_max_sweep_retries = 5
        settings.marketplace_withdrawal_cooldown_seconds = 0  # disable cooldown for test
        settings.agent_wallet_enabled = True
        settings.marketplace_settlement_cdp_account = "td-marketplace"
        settings.marketplace_settlement_chain_id = 84532
        mock_settings.return_value = settings

        count = await marketplace_sweep_once()

    assert count == 1

    # Confirm earnings are now settled
    status_row = await pool.fetchrow(
        "SELECT status FROM tool_author_earnings WHERE org_id = $1",
        org.id,
    )
    assert status_row["status"] == "settled"

    # Confirm withdrawal is settled
    wd_row = await pool.fetchrow(
        "SELECT status, tx_hash FROM tool_author_withdrawals WHERE org_id = $1",
        org.id,
    )
    assert wd_row["status"] == "settled"
    assert wd_row["tx_hash"] == "0xtxhash"


@pytest.mark.anyio
async def test_sweep_cdp_failure_sets_backoff(sweep_db_pool):
    """If CDP raises, the withdrawal is marked failed with next_sweep_at set."""
    pool = sweep_db_pool

    org = await create_org("sweep-fail-org")
    await set_author_config(org.id, settlement_wallet=_VALID_ADDR)
    await _seed_org_with_earnings(pool, org.id, 500_000)

    with (
        patch("marketplace.get_settings") as mock_settings,
        patch("agent_wallets.transfer_usdc", new=AsyncMock(side_effect=RuntimeError("CDP down"))),
    ):
        settings = MagicMock()
        settings.marketplace_minimum_withdrawal_usdc = 100_000
        settings.marketplace_max_sweep_retries = 5
        settings.marketplace_withdrawal_cooldown_seconds = 0
        settings.agent_wallet_enabled = True
        settings.marketplace_settlement_cdp_account = "td-marketplace"
        settings.marketplace_settlement_chain_id = 84532
        mock_settings.return_value = settings

        count = await marketplace_sweep_once()

    assert count == 0

    wd_row = await pool.fetchrow(
        "SELECT status, sweep_attempt_count, next_sweep_at, last_sweep_error FROM tool_author_withdrawals WHERE org_id = $1",
        org.id,
    )
    assert wd_row["status"] == "failed"
    assert wd_row["sweep_attempt_count"] == 1
    assert wd_row["next_sweep_at"] is not None
    assert wd_row["last_sweep_error"] != ""


@pytest.mark.anyio
async def test_sweep_retry_admin_endpoint_resets_exhausted(sweep_db_pool):
    """reset_withdrawal should reset an exhausted withdrawal back to pending."""
    pool = sweep_db_pool

    org = await create_org("sweep-exhaust-org")
    await set_author_config(org.id, settlement_wallet=_VALID_ADDR)

    # Insert an exhausted withdrawal directly
    wd_id = "wd-exhausted-test"
    await pool.execute(
        """
        INSERT INTO tool_author_withdrawals
            (id, org_id, amount_usdc, wallet, status, created_at,
             sweep_attempt_count, last_sweep_error, next_sweep_at)
        VALUES ($1, $2, 500000, $3, 'exhausted', NOW(), 5, 'CDP unavailable', NULL)
        """,
        wd_id,
        org.id,
        _VALID_ADDR,
    )

    found = await reset_withdrawal(wd_id)
    assert found is True

    row = await pool.fetchrow(
        "SELECT status, sweep_attempt_count, next_sweep_at FROM tool_author_withdrawals WHERE id = $1",
        wd_id,
    )
    assert row["status"] == "pending"
    assert row["sweep_attempt_count"] == 0
    assert row["next_sweep_at"] is None


@pytest.mark.anyio
async def test_sweep_is_idempotent_on_restart(sweep_db_pool):
    """Running sweep twice in the same epoch hour processes the org only once."""
    pool = sweep_db_pool

    org = await create_org("sweep-idempotent-org")
    await set_author_config(org.id, settlement_wallet=_VALID_ADDR)
    await _seed_org_with_earnings(pool, org.id, 500_000)

    with (
        patch("marketplace.get_settings") as mock_settings,
        patch("agent_wallets.transfer_usdc", new=AsyncMock(return_value="0xtxhash2")),
    ):
        settings = MagicMock()
        settings.marketplace_minimum_withdrawal_usdc = 100_000
        settings.marketplace_max_sweep_retries = 5
        settings.marketplace_withdrawal_cooldown_seconds = 0
        settings.agent_wallet_enabled = True
        settings.marketplace_settlement_cdp_account = "td-marketplace"
        settings.marketplace_settlement_chain_id = 84532
        mock_settings.return_value = settings

        count1 = await marketplace_sweep_once()
        count2 = await marketplace_sweep_once()

    assert count1 == 1
    # Second call: earnings already settled → org excluded by the subquery
    assert count2 == 0

    # Exactly one withdrawal row should exist
    wd_count = await pool.fetchval(
        "SELECT COUNT(*) FROM tool_author_withdrawals WHERE org_id = $1",
        org.id,
    )
    assert wd_count == 1


@pytest.mark.anyio
async def test_sweep_tx_reverted_marks_failed(sweep_db_pool):
    """If verify_usdc_transfer returns False (reverted tx), withdrawal is marked failed."""
    pool = sweep_db_pool

    org = await create_org("sweep-revert-org")
    await set_author_config(org.id, settlement_wallet=_VALID_ADDR)
    await _seed_org_with_earnings(pool, org.id, 500_000)

    with (
        patch("marketplace.get_settings") as mock_settings,
        patch("agent_wallets.transfer_usdc", new=AsyncMock(return_value="0xreverted")),
        patch(
            "agent_wallets.verify_usdc_transfer",
            new=AsyncMock(return_value=False),
        ),
    ):
        settings = MagicMock()
        settings.marketplace_minimum_withdrawal_usdc = 100_000
        settings.marketplace_max_sweep_retries = 5
        settings.marketplace_withdrawal_cooldown_seconds = 0
        settings.agent_wallet_enabled = True
        settings.marketplace_settlement_cdp_account = "td-marketplace"
        settings.marketplace_settlement_chain_id = 84532
        settings.marketplace_tx_confirm_timeout_seconds = 5
        mock_settings.return_value = settings

        count = await marketplace_sweep_once()

    assert count == 0

    wd_row = await pool.fetchrow(
        "SELECT status, last_sweep_error FROM tool_author_withdrawals WHERE org_id = $1",
        org.id,
    )
    assert wd_row["status"] == "failed"
    assert "reverted" in wd_row["last_sweep_error"]

    # Earnings should be reverted back to pending so the next sweep can retry.
    earnings_row = await pool.fetchrow(
        "SELECT status FROM tool_author_earnings WHERE org_id = $1",
        org.id,
    )
    assert earnings_row["status"] == "pending"


@pytest.mark.anyio
async def test_sweep_tx_verification_skipped_when_no_rpc_url(sweep_db_pool):
    """ValueError from verify_usdc_transfer (no RPC URL) → proceeds optimistically as settled."""
    pool = sweep_db_pool

    org = await create_org("sweep-no-rpc-org")
    await set_author_config(org.id, settlement_wallet=_VALID_ADDR)
    await _seed_org_with_earnings(pool, org.id, 500_000)

    with (
        patch("marketplace.get_settings") as mock_settings,
        patch("agent_wallets.transfer_usdc", new=AsyncMock(return_value="0xtxhash_norpc")),
        patch(
            "agent_wallets.verify_usdc_transfer",
            new=AsyncMock(side_effect=ValueError("No RPC URL available")),
        ),
    ):
        settings = MagicMock()
        settings.marketplace_minimum_withdrawal_usdc = 100_000
        settings.marketplace_max_sweep_retries = 5
        settings.marketplace_withdrawal_cooldown_seconds = 0
        settings.agent_wallet_enabled = True
        settings.marketplace_settlement_cdp_account = "td-marketplace"
        settings.marketplace_settlement_chain_id = 84532
        settings.marketplace_tx_confirm_timeout_seconds = 5
        mock_settings.return_value = settings

        count = await marketplace_sweep_once()

    assert count == 1

    wd_row = await pool.fetchrow(
        "SELECT status, tx_hash FROM tool_author_withdrawals WHERE org_id = $1",
        org.id,
    )
    assert wd_row["status"] == "settled"
    assert wd_row["tx_hash"] == "0xtxhash_norpc"


@pytest.mark.anyio
async def test_sweep_balance_warning_logged_on_low_balance(sweep_db_pool, caplog):
    """Settlement wallet below threshold → logger.error emitted after sweep cycle."""
    import logging

    pool = sweep_db_pool

    org = await create_org("sweep-balance-warn-org")
    await set_author_config(org.id, settlement_wallet=_VALID_ADDR)
    await _seed_org_with_earnings(pool, org.id, 500_000)

    with (
        patch("marketplace.get_settings") as mock_settings,
        patch("agent_wallets.transfer_usdc", new=AsyncMock(return_value="0xtxhash3")),
        patch("agent_wallets.verify_usdc_transfer", new=AsyncMock(return_value=True)),
        patch(
            "agent_wallets.get_settlement_wallet_balance_usdc",
            new=AsyncMock(return_value=500_000),  # $0.50 — below $5.00 threshold
        ),
    ):
        settings = MagicMock()
        settings.marketplace_minimum_withdrawal_usdc = 100_000
        settings.marketplace_max_sweep_retries = 5
        settings.marketplace_withdrawal_cooldown_seconds = 0
        settings.agent_wallet_enabled = True
        settings.marketplace_settlement_cdp_account = "td-marketplace"
        settings.marketplace_settlement_chain_id = 84532
        settings.marketplace_tx_confirm_timeout_seconds = 5
        settings.marketplace_settlement_warn_threshold_usdc = 5_000_000  # $5.00
        mock_settings.return_value = settings

        with caplog.at_level(logging.ERROR, logger="marketplace"):
            count = await marketplace_sweep_once()

    assert count == 1
    assert any("settlement wallet below threshold" in r.message and r.levelno == logging.ERROR for r in caplog.records)
