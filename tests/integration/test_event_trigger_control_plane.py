# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Postgres integration coverage for event-dispatch leases and bounded chains."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from migrations.runner import apply_pending
from scheduling import (
    close_scheduling_db,
    create_event_trigger,
    get_event_dispatch_lineage,
    init_scheduling_db,
    list_scheduled_run_results,
    recover_expired_event_dispatches,
    reserve_event_dispatch_lease,
    start_event_dispatch_lease,
)
from scheduling.crud import record_scheduled_run_result
from shared.db_pool import CheckViolation, UniqueViolation, create_pool


@pytest.fixture
async def event_control_pool(docker_postgres: str):
    pool = await create_pool(docker_postgres, min_size=1, max_size=5, name="integration-event-trigger")
    await apply_pending(pool)
    await pool.execute("TRUNCATE scheduled_runs, scheduled_run_results, event_trigger_events CASCADE")
    await init_scheduling_db(pool)
    try:
        yield pool
    finally:
        await pool.execute("TRUNCATE scheduled_runs, scheduled_run_results, event_trigger_events CASCADE")
        await close_scheduling_db()
        await pool.close()


@pytest.mark.asyncio
async def test_dispatch_admission_duplicate_saturation_and_recovery(event_control_pool):
    pool = event_control_pool
    await pool.execute(
        """
        INSERT INTO scheduled_runs (
            id, org_id, user_id, name, prompt, schedule_kind, interval_seconds,
            enabled, trigger_token, secret_hash, next_run_at, created_at, updated_at
        )
        VALUES (
            'event-1', 'org-1', 'user-1', 'Event', 'Handle {{kind}}', 'event', NULL,
            TRUE, 'trigger-1', 'secret-hash', NULL, NOW(), NOW()
        )
        """
    )

    accepted = await reserve_event_dispatch_lease(
        schedule_id="event-1",
        org_id="org-1",
        idempotency_key="source-event-1",
        run_id="run-1",
        chain_id="run-1",
        hop_index=0,
        owner_id="worker-1",
        lease_seconds=180,
        global_limit=1,
        org_limit=1,
    )
    duplicate = await reserve_event_dispatch_lease(
        schedule_id="event-1",
        org_id="org-1",
        idempotency_key="source-event-1",
        run_id="run-duplicate",
        chain_id="run-duplicate",
        hop_index=0,
        owner_id="worker-2",
        lease_seconds=180,
        global_limit=1,
        org_limit=1,
    )
    saturated = await reserve_event_dispatch_lease(
        schedule_id="event-1",
        org_id="org-1",
        idempotency_key="source-event-2",
        run_id="run-2",
        chain_id="run-2",
        hop_index=0,
        owner_id="worker-2",
        lease_seconds=180,
        global_limit=1,
        org_limit=1,
    )

    assert accepted.outcome == "accepted"
    assert duplicate == type(duplicate)(run_id="run-1", outcome="duplicate")
    assert saturated.outcome == "saturated"
    assert await start_event_dispatch_lease("run-1", "worker-1", 180) is True

    await pool.execute("UPDATE event_dispatch_leases SET lease_expires_at = NOW() - INTERVAL '1 second' WHERE run_id = 'run-1'")
    assert await recover_expired_event_dispatches(limit=10, max_consecutive_failures=5) == 1

    lease = await pool.fetchrow("SELECT status, last_error FROM event_dispatch_leases WHERE run_id = 'run-1'")
    result = await pool.fetchrow("SELECT status, error FROM scheduled_run_results WHERE run_id = 'run-1'")
    events = await pool.fetch("SELECT event_type, status FROM event_trigger_events WHERE run_id = 'run-1' ORDER BY created_at")
    assert dict(lease) == {"status": "failed", "last_error": "Event run recovery timed out."}
    assert dict(result) == {"status": "failed", "error": "Event run recovery timed out."}
    assert [dict(row) for row in events] == [
        {"event_type": "dispatch_accepted", "status": ""},
        {"event_type": "run_settled", "status": "failed"},
    ]


async def _create_trigger(name: str):
    return await create_event_trigger(
        org_id="org-1",
        user_id="user-1",
        name=name,
        prompt="{{output_text}}",
        callback_url=None,
        trigger_token=str(uuid.uuid4()),
        secret_hash="hash",
    )


async def _reserve(schedule_id: str, run_id: str, chain_id: str, hop_index: int):
    return await reserve_event_dispatch_lease(
        schedule_id=schedule_id,
        org_id="org-1",
        idempotency_key=run_id,
        run_id=run_id,
        owner_id="worker-1",
        lease_seconds=180,
        global_limit=100,
        org_limit=100,
        chain_id=chain_id,
        hop_index=hop_index,
    )


@pytest.mark.asyncio
async def test_duplicate_chain_hop_rolls_back_dispatch(event_control_pool):
    first = await _create_trigger("first")
    second = await _create_trigger("second")
    await _reserve(first.id, "child-1", "root", 1)

    with pytest.raises(UniqueViolation):
        await _reserve(second.id, "child-2", "root", 1)

    assert await get_event_dispatch_lineage(second.id, "child-2") is None
    assert await event_control_pool.fetchval("SELECT COUNT(*) FROM event_dispatch_leases WHERE run_id='child-2'") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("chain_id,hop_index", [("root", -1), ("other-root", 0)])
async def test_invalid_chain_hop_rolls_back_dispatch(chain_id, hop_index, event_control_pool):
    schedule = await _create_trigger("invalid-hop")

    with pytest.raises(CheckViolation):
        await _reserve(schedule.id, "run-1", chain_id, hop_index)

    assert await get_event_dispatch_lineage(schedule.id, "run-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("hop_index", [3, 32_767])
async def test_database_accepts_hops_above_original_policy_cap(hop_index, event_control_pool):
    schedule = await _create_trigger("higher-hop")

    reservation = await _reserve(schedule.id, "child", "root", hop_index)

    assert reservation.outcome == "accepted"
    assert await get_event_dispatch_lineage(schedule.id, "child") == ("root", hop_index)


@pytest.mark.asyncio
async def test_chain_lineage_results_preserve_cursor_and_org_scope(event_control_pool):
    schedule = await _create_trigger("results")
    await _reserve(schedule.id, "root", "root", 0)
    await _reserve(schedule.id, "child", "root", 1)
    for run_id in ("root", "child"):
        await record_scheduled_run_result(
            schedule_id=schedule.id,
            org_id="org-1",
            run_id=run_id,
            status="completed",
            output_text=run_id,
            cost_usdc=0,
            error="",
        )

    results = await list_scheduled_run_results(schedule.id, "org-1", limit=1)
    assert [(r.run_id, r.chain_id, r.hop_index) for r in results] == [("child", "root", 1)]
    earlier = await list_scheduled_run_results(schedule.id, "org-1", cursor=results[0].created_at)
    assert [(r.run_id, r.chain_id, r.hop_index) for r in earlier] == [("root", "root", 0)]
    assert await list_scheduled_run_results(schedule.id, "foreign-org") == []
    assert await list_scheduled_run_results(schedule.id, "org-1", cursor=datetime.now(timezone.utc) - timedelta(days=1)) == []


@pytest.mark.asyncio
async def test_migration_backfills_legacy_dispatch_lineage(event_control_pool):
    schema = f"chain_migration_{uuid.uuid4().hex}"
    migration = Path(__file__).resolve().parents[2] / "migrations/versions/129_event_trigger_chains.sql"
    async with event_control_pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(f"CREATE SCHEMA {schema}; SET LOCAL search_path TO {schema}")
            await connection.execute(
                "CREATE TABLE scheduled_runs (id TEXT PRIMARY KEY, schedule_kind TEXT); "
                "CREATE TABLE event_dispatch_keys (schedule_id TEXT, idempotency_key TEXT, run_id TEXT); "
                "CREATE TABLE event_trigger_events (event_type TEXT CHECK (event_type IN ('run_settled')))"
            )
            await connection.execute("INSERT INTO event_dispatch_keys VALUES ('legacy-trigger', 'legacy-key', 'legacy-run')")
            await connection.execute(migration.read_text(encoding="utf-8"))
            await connection.execute(
                "INSERT INTO event_dispatch_keys (schedule_id, idempotency_key, run_id, chain_id, hop_index) "
                "VALUES ('child-trigger', 'child-key', 'child-run', 'legacy-run', 3)"
            )
            await connection.execute(migration.read_text(encoding="utf-8"))

            rows = await connection.fetch("SELECT run_id, chain_id, hop_index FROM event_dispatch_keys ORDER BY hop_index")
            assert [dict(row) for row in rows] == [
                {"run_id": "legacy-run", "chain_id": "legacy-run", "hop_index": 0},
                {"run_id": "child-run", "chain_id": "legacy-run", "hop_index": 3},
            ]
            await connection.execute("INSERT INTO event_trigger_events VALUES ('chain_dispatch_skipped')")
            await connection.execute(f"DROP SCHEMA {schema} CASCADE")
