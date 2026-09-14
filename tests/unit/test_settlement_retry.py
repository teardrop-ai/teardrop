# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import billing.settlement as settlement


@pytest.mark.anyio
async def test_pending_settlements_are_atomically_leased_before_processing():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])

    with patch.object(settlement, "_get_pool", return_value=pool):
        assert await settlement.process_pending_settlements() == 0

    claim_sql = pool.fetch.await_args.args[0]
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "UPDATE pending_settlements AS settlement" in claim_sql
    assert "SET status = 'retrying'" in claim_sql
    assert "next_retry_at = NOW() + INTERVAL '5 minutes'" in claim_sql
    assert "RETURNING settlement.id" in claim_sql
    # The lease token guards every follow-up write, so it must be returned by the claim.
    assert "settlement.next_retry_at" in claim_sql.split("RETURNING", 1)[1]


@pytest.mark.anyio
async def test_credit_debit_and_retry_finalization_share_one_transaction():
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)

    connection = MagicMock()
    connection.transaction.return_value = transaction
    connection.fetchval = AsyncMock(return_value="settlement-1")
    connection.execute = AsyncMock()

    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=connection)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire

    credit_service = MagicMock()
    credit_service._debit_credit_locked = AsyncMock(return_value=(True, 500, "ledger-1"))
    spend_cache = MagicMock()
    spend_cache.invalidate = AsyncMock()
    row = {
        "id": "settlement-1",
        "usage_event_id": "usage-1",
        "org_id": "org-1",
        "run_id": "run-1",
        "amount_usdc": 500,
        "principal_id": "principal-1",
        "next_retry_at": "2026-09-14T12:05:00Z",
    }

    with (
        patch.object(settlement, "_get_credit_service", return_value=credit_service),
        patch.object(settlement, "_get_daily_spend_cache", return_value=spend_cache),
    ):
        assert await settlement._settle_claimed_credit(pool, row, 2) == (True, 500)

    credit_service._debit_credit_locked.assert_awaited_once_with(
        connection,
        "org-1",
        500,
        reason="run:run-1",
        principal_id="principal-1",
    )
    assert connection.execute.await_count == 2
    assert "UPDATE pending_settlements" in connection.execute.await_args_list[0].args[0]
    assert "UPDATE usage_events" in connection.execute.await_args_list[1].args[0]
    transaction.__aexit__.assert_awaited_once()


@pytest.mark.anyio
async def test_settle_claimed_credit_raises_when_claim_is_lost():
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)

    connection = MagicMock()
    connection.transaction.return_value = transaction
    connection.fetchval = AsyncMock(return_value=None)  # Claim expired or stolen

    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=connection)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire

    credit_service = MagicMock()
    credit_service._debit_credit_locked = AsyncMock()

    row = {
        "id": "settlement-1",
        "usage_event_id": "usage-1",
        "org_id": "org-1",
        "run_id": "run-1",
        "amount_usdc": 500,
        "principal_id": "principal-1",
        "next_retry_at": "2026-09-14T12:05:00Z",
    }

    with patch.object(settlement, "_get_credit_service", return_value=credit_service):
        with pytest.raises(settlement.SettlementClaimLostError):
            await settlement._settle_claimed_credit(pool, row, 2)

    credit_service._debit_credit_locked.assert_not_awaited()


@pytest.mark.anyio
async def test_process_pending_settlements_abandons_lost_claims_without_clobbering():
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {
                "id": "settlement-1",
                "usage_event_id": "usage-1",
                "org_id": "org-1",
                "run_id": "run-1",
                "billing_method": "credit",
                "amount_usdc": 500,
                "payment_payload": None,
                "principal_id": "principal-1",
                "retry_count": 0,
                "max_retries": 3,
                "next_retry_at": "2026-09-14T12:05:00Z",
            }
        ]
    )
    pool.execute = AsyncMock()

    with (
        patch.object(settlement, "_get_pool", return_value=pool),
        patch.object(
            settlement,
            "_settle_claimed_credit",
            side_effect=settlement.SettlementClaimLostError("lost"),
        ),
    ):
        processed = await settlement.process_pending_settlements()

    assert processed == 0
    # Crucial: pool.execute must NOT be called to update/exhaust/revert the lost row!
    pool.execute.assert_not_awaited()
