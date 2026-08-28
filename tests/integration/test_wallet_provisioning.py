# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Real-Postgres concurrency coverage for machine wallet provisioning."""

from __future__ import annotations

import asyncio

import pytest

import teardrop.users as user_module
import teardrop.wallets as wallet_module
from migrations.runner import apply_pending
from shared.db_pool import create_pool
from teardrop.wallets import provision_org_for_wallet

_ADDRESS = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


@pytest.fixture
async def provisioning_db_pool(docker_postgres, test_settings):
    pool = await create_pool(docker_postgres, min_size=1, max_size=5, name="integration-wallet-provisioning")
    await apply_pending(pool)
    user_module.base._pool = pool
    wallet_module._pool = pool
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE org_provisioning_events, org_credit_ledger, org_credits, "
            "org_client_credentials, siwe_nonces, wallets, users, orgs RESTART IDENTITY CASCADE"
        )
    yield pool
    user_module.base._pool = None
    wallet_module._pool = None
    await pool.close()


@pytest.mark.anyio
async def test_concurrent_wallet_provisioning_is_singleton(provisioning_db_pool):
    results = await asyncio.gather(*(provision_org_for_wallet(_ADDRESS, 1, acquisition_source="siwe") for _ in range(8)))

    assert sum(result.created for result in results) == 1
    assert len({result.org.id for result in results}) == 1
    assert len({result.user.id for result in results}) == 1
    assert len({result.wallet.id for result in results}) == 1

    counts = await provisioning_db_pool.fetchrow(
        """
        SELECT
            (SELECT COUNT(*) FROM orgs) AS org_count,
            (SELECT COUNT(*) FROM users) AS user_count,
            (SELECT COUNT(*) FROM wallets) AS wallet_count,
            (SELECT spending_limit_usdc FROM org_credits WHERE org_id = $1) AS spend_limit
        """,
        results[0].org.id,
    )
    assert counts["org_count"] == 1
    assert counts["user_count"] == 1
    assert counts["wallet_count"] == 1
    assert counts["spend_limit"] == 5_000_000
