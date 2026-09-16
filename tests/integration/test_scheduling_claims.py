# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Postgres integration coverage for interval schedule claiming."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from migrations.runner import apply_pending
from scheduling import close_scheduling_db, init_scheduling_db
from scheduling.crud import claim_due_schedules
from shared.db_pool import create_pool


@pytest.fixture
async def scheduling_pool(docker_postgres: str):
    pool = await create_pool(docker_postgres, min_size=1, max_size=5, name="integration-scheduling-claims")
    await apply_pending(pool)
    await init_scheduling_db(pool)
    await pool.execute("TRUNCATE TABLE scheduled_runs RESTART IDENTITY CASCADE")

    yield pool

    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE scheduled_runs RESTART IDENTITY CASCADE")
    await close_scheduling_db()
    await pool.close()


@pytest.mark.asyncio
async def test_due_interval_schedule_is_claimed_once_concurrently(scheduling_pool):
    due_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await scheduling_pool.execute(
        """
        INSERT INTO scheduled_runs (
            id, org_id, user_id, name, prompt, schedule_kind, interval_seconds,
            enabled, next_run_at, created_at, updated_at
        )
        VALUES ('interval-claim-1', 'org-1', 'user-1', 'Interval', 'Run once', 'interval', 60,
                TRUE, $1, $1, $1)
        """,
        due_at,
    )

    claim_batches = await asyncio.gather(
        claim_due_schedules(limit=1),
        claim_due_schedules(limit=1),
    )
    claimed = [schedule for batch in claim_batches for schedule in batch]

    assert [schedule.id for schedule in claimed] == ["interval-claim-1"]
    assert claimed[0].next_run_at > due_at

    stored = await scheduling_pool.fetchrow(
        "SELECT next_run_at FROM scheduled_runs WHERE id = $1",
        "interval-claim-1",
    )
    assert stored["next_run_at"] > due_at
