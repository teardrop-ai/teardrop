"""Postgres integration coverage for multi-instance event-dispatch leases."""

from __future__ import annotations

import pytest

from migrations.runner import apply_pending
from scheduling import (
    close_scheduling_db,
    init_scheduling_db,
    recover_expired_event_dispatches,
    reserve_event_dispatch_lease,
    start_event_dispatch_lease,
)
from shared.db_pool import create_pool


@pytest.fixture
async def event_control_pool(docker_postgres: str):
    pool = await create_pool(docker_postgres, min_size=1, max_size=5, name="integration-event-trigger")
    await apply_pending(pool)
    await init_scheduling_db(pool)
    yield pool
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
