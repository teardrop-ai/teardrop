from __future__ import annotations

import asyncpg
import pytest

import billing.context as billing_context
from migrations.runner import apply_pending
from shared.audit import insert_event_row
from teardrop.routers.a2a_messages import _A2A_INBOUND_EVENT_INSERT_SQL, _record_inbound_event


@pytest.fixture
async def audit_db_pool(docker_postgres: str):
    pool = await asyncpg.create_pool(docker_postgres, min_size=1, max_size=5)
    await apply_pending(pool)
    billing_context._bind_pool(pool)

    yield pool

    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE a2a_inbound_events RESTART IDENTITY CASCADE")

    billing_context._clear_pool()
    await pool.close()


@pytest.mark.parametrize(
    ("task_state", "billing_method", "error"),
    [
        ("completed", "x402", ""),
        ("failed", "x402", "Task failed."),
        ("timeout", "x402", "Task timed out."),
        ("rejected_payment", "x402", "Payment verification failed"),
        ("rejected_auth_credit", "credit", "Insufficient credits"),
    ],
)
async def test_record_inbound_event_persists_valid_states(audit_db_pool, task_state: str, billing_method: str, error: str):
    run_id = f"run-{task_state}"

    await _record_inbound_event(
        run_id=run_id,
        usage_event_id="evt-1",
        caller_org_id="org-1",
        caller_user_id="user-1",
        caller_address="0xabc",
        caller_ip="198.51.100.7",
        auth_method="anonymous",
        context_id="ctx-1",
        task_id="task-1",
        task_state=task_state,
        cost_usdc=12_345,
        settlement_tx="0xtx",
        billing_method=billing_method,
        duration_ms=250,
        error=error,
    )

    row = await audit_db_pool.fetchrow(
        """
        SELECT run_id, usage_event_id, caller_org_id, caller_user_id, caller_address,
               caller_ip, auth_method, context_id, task_id, task_state, cost_usdc,
               settlement_tx, billing_method, duration_ms, error
        FROM a2a_inbound_events
        WHERE run_id = $1
        """,
        run_id,
    )

    assert row is not None
    assert row["run_id"] == run_id
    assert row["usage_event_id"] == "evt-1"
    assert row["caller_org_id"] == "org-1"
    assert row["caller_user_id"] == "user-1"
    assert row["caller_address"] == "0xabc"
    assert row["caller_ip"] == "198.51.100.7"
    assert row["auth_method"] == "anonymous"
    assert row["context_id"] == "ctx-1"
    assert row["task_id"] == "task-1"
    assert row["task_state"] == task_state
    assert row["cost_usdc"] == 12_345
    assert row["settlement_tx"] == "0xtx"
    assert row["billing_method"] == billing_method
    assert row["duration_ms"] == 250
    assert row["error"] == error


async def test_record_inbound_event_appends_rows_for_same_run(audit_db_pool):
    for task_state in ("failed", "completed"):
        await _record_inbound_event(
            run_id="run-shared",
            usage_event_id="evt-1",
            caller_org_id="org-1",
            caller_user_id="user-1",
            caller_address="",
            caller_ip="198.51.100.7",
            auth_method="anonymous",
            context_id="ctx-1",
            task_id="task-1",
            task_state=task_state,
            cost_usdc=0,
            settlement_tx="",
            billing_method="x402",
            duration_ms=100,
            error="",
        )

    count = await audit_db_pool.fetchval(
        "SELECT COUNT(*) FROM a2a_inbound_events WHERE run_id = $1",
        "run-shared",
    )

    assert count == 2


async def test_a2a_inbound_events_check_constraint_rejects_invalid_task_state(audit_db_pool):
    with pytest.raises(asyncpg.CheckViolationError):
        await insert_event_row(
            audit_db_pool,
            insert_sql=_A2A_INBOUND_EVENT_INSERT_SQL,
            values=(
                "run-invalid",
                "evt-1",
                "org-1",
                "user-1",
                "",
                "198.51.100.7",
                "anonymous",
                "ctx-1",
                "task-1",
                "invalid_state",
                0,
                "",
                "x402",
                0,
                "",
            ),
        )
