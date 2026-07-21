"""Integration coverage for disposable-data retention on Postgres."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest

from migrations.runner import apply_pending
from teardrop.retention import close_retention_db, init_retention_db, retention_sweep_once


@pytest.fixture
async def retention_db_pool(docker_postgres: str):
    pool = await asyncpg.create_pool(docker_postgres, min_size=1, max_size=5)
    await apply_pending(pool)
    await init_retention_db(pool)
    yield pool
    await close_retention_db()
    await pool.close()


def _retention_settings() -> SimpleNamespace:
    return SimpleNamespace(
        checkpoint_ttl_days=45,
        scheduled_run_results_ttl_days=30,
        org_tool_execution_events_ttl_days=90,
        telemetry_run_starts_ttl_days=120,
        retention_sweep_batch_size=10,
    )


@pytest.mark.asyncio
async def test_retention_sweeps_disposable_records_but_keeps_immutable_data(retention_db_pool):
    old = datetime.now(timezone.utc) - timedelta(days=130)
    pool = retention_db_pool

    await pool.execute(
        """
        INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata)
        VALUES ($1, '', 'checkpoint-1', $2::jsonb, '{}'::jsonb)
        """,
        "user-1:thread-1",
        json.dumps({}),
    )
    await pool.execute(
        """
        INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob)
        VALUES ($1, '', 'messages', 'v1', 'json', $2)
        """,
        "user-1:thread-1",
        b"state",
    )
    await pool.execute(
        """
        INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, blob)
        VALUES ($1, '', 'checkpoint-1', 'task-1', 0, 'messages', $2)
        """,
        "user-1:thread-1",
        b"write",
    )
    await pool.execute(
        """
        INSERT INTO checkpoint_thread_activity (thread_id, last_activity_at)
        VALUES ($1, $2)
        """,
        "user-1:thread-1",
        old,
    )

    await pool.execute(
        """
        INSERT INTO scheduled_runs (
            id, org_id, user_id, name, prompt, schedule_kind, interval_seconds,
            enabled, next_run_at, created_at, updated_at
        )
        VALUES ('schedule-1', 'org-1', 'user-1', 'Daily', 'Summarize', 'interval', 3600, TRUE, $1, $1, $1)
        """,
        old,
    )
    await pool.execute(
        """
        INSERT INTO scheduled_run_results (id, schedule_id, org_id, run_id, status, output_text, cost_usdc, created_at)
        VALUES ('result-1', 'schedule-1', 'org-1', 'scheduled-run-1', 'completed', 'old output', 123, $1)
        """,
        old,
    )
    await pool.execute(
        """
        INSERT INTO org_tool_events (id, org_id, tool_id, tool_name, event_type, actor_id, created_at)
        VALUES ('tool-event-1', 'org-1', 'tool-1', 'weather', 'executed', 'user-1', $1)
        """,
        old,
    )
    await pool.execute(
        """
        INSERT INTO siwe_login_sessions (session_id, nonce, status, created_at, expires_at)
        VALUES ('siwe-session-1', 'nonce-1', 'pending', $1, $1)
        """,
        old,
    )

    await pool.execute(
        """
        INSERT INTO usage_events (id, user_id, org_id, thread_id, run_id, created_at)
        VALUES ('usage-1', 'user-1', 'org-1', 'thread-1', 'usage-run-1', $1)
        """,
        old,
    )
    await pool.execute(
        """
        INSERT INTO tool_call_events (id, run_id, org_id, tool_name, created_at)
        VALUES ('tool-call-1', 'usage-run-1', 'org-1', 'weather', $1)
        """,
        old,
    )
    await pool.execute(
        """
        INSERT INTO run_decisions (id, run_id, org_id, user_id, created_at)
        VALUES ('decision-1', 'usage-run-1', 'org-1', 'user-1', $1)
        """,
        old,
    )
    await pool.execute(
        """
        INSERT INTO a2a_inbound_events (id, task_state, created_at)
        VALUES ('a2a-event-1', 'completed', $1)
        """,
        old,
    )
    await pool.execute(
        """
        INSERT INTO telemetry_run_starts (run_id, org_id, source, started_at)
        VALUES ('telemetry-run-1', 'org-1', 'api', $1)
        """,
        old,
    )

    result = await retention_sweep_once(_retention_settings())

    assert result.checkpoint_threads == 1
    assert result.scheduled_run_results == 1
    assert result.org_tool_execution_events == 1
    assert result.telemetry_run_starts == 1
    assert result.expired_siwe_login_sessions == 1
    assert await pool.fetchval("SELECT COUNT(*) FROM checkpoints") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM checkpoint_blobs") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM checkpoint_writes") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM scheduled_run_results") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM org_tool_events WHERE event_type = 'executed'") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM siwe_login_sessions") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM usage_events") == 1
    assert await pool.fetchval("SELECT COUNT(*) FROM tool_call_events") == 1
    assert await pool.fetchval("SELECT COUNT(*) FROM run_decisions") == 1
    assert await pool.fetchval("SELECT COUNT(*) FROM a2a_inbound_events") == 1
    assert await pool.fetchval("SELECT COUNT(*) FROM telemetry_run_starts") == 0
