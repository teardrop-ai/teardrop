# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import scheduling.crud as crud


def _pool_with_connection(connection: MagicMock) -> MagicMock:
    @asynccontextmanager
    async def acquire():
        yield connection

    @asynccontextmanager
    async def transaction():
        yield

    connection.transaction = transaction
    pool = MagicMock()
    pool.acquire = acquire
    return pool


@pytest.mark.anyio
async def test_reserve_event_dispatch_lease_returns_duplicate_before_capacity_check():
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.fetchval = AsyncMock(return_value="existing-run")
    pool = _pool_with_connection(connection)

    with patch.object(crud, "_get_pool", return_value=pool):
        result = await crud.reserve_event_dispatch_lease(
            schedule_id="schedule-1",
            org_id="org-1",
            idempotency_key="event-1",
            run_id="new-run",
            owner_id="worker-1",
            lease_seconds=180,
            global_limit=8,
            org_limit=4,
        )

    assert result.outcome == "duplicate"
    assert result.run_id == "existing-run"
    assert connection.fetchval.await_count == 1


@pytest.mark.anyio
async def test_reserve_event_dispatch_lease_rejects_global_saturation():
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.fetchval = AsyncMock(side_effect=[None, 8])
    pool = _pool_with_connection(connection)

    with patch.object(crud, "_get_pool", return_value=pool):
        result = await crud.reserve_event_dispatch_lease(
            schedule_id="schedule-1",
            org_id="org-1",
            idempotency_key="event-1",
            run_id="run-1",
            owner_id="worker-1",
            lease_seconds=180,
            global_limit=8,
            org_limit=4,
        )

    assert result.outcome == "saturated"
    assert connection.execute.await_count == 1


@pytest.mark.anyio
async def test_reserve_event_dispatch_lease_commits_identity_lease_and_audit():
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.fetchval = AsyncMock(side_effect=[None, 0, 0])
    pool = _pool_with_connection(connection)

    with (
        patch.object(crud, "_get_pool", return_value=pool),
        patch.object(crud, "insert_event_row", AsyncMock()) as audit_insert,
    ):
        result = await crud.reserve_event_dispatch_lease(
            schedule_id="schedule-1",
            org_id="org-1",
            idempotency_key="event-1",
            run_id="run-1",
            owner_id="worker-1",
            lease_seconds=180,
            global_limit=8,
            org_limit=4,
        )

    assert result.outcome == "accepted"
    executed_sql = "\n".join(call.args[0] for call in connection.execute.await_args_list)
    assert "INSERT INTO event_dispatch_keys" in executed_sql
    assert "INSERT INTO event_dispatch_leases" in executed_sql
    audit_insert.assert_awaited_once()
    assert audit_insert.await_args.kwargs["values"][3] == "dispatch_accepted"


@pytest.mark.anyio
async def test_recovery_reconciles_existing_result_without_reexecution():
    connection = MagicMock()
    connection.fetch = AsyncMock(
        return_value=[
            {
                "run_id": "run-1",
                "schedule_id": "schedule-1",
                "org_id": "org-1",
                "owner_id": "worker-1",
            }
        ]
    )
    connection.fetchrow = AsyncMock(
        return_value={
            "id": "result-1",
            "schedule_id": "schedule-1",
            "org_id": "org-1",
            "run_id": "run-1",
            "status": "completed",
            "output_text": "done",
            "cost_usdc": 100,
            "error": "",
            "created_at": crud.datetime.now(crud.timezone.utc),
        }
    )
    connection.execute = AsyncMock()
    pool = _pool_with_connection(connection)

    with (
        patch.object(crud, "_get_pool", return_value=pool),
        patch.object(crud, "insert_event_row", AsyncMock()) as audit_insert,
    ):
        recovered = await crud.recover_expired_event_dispatches(limit=10, max_consecutive_failures=5)

    assert recovered == 1
    update_call = next(call for call in connection.execute.await_args_list if "UPDATE event_dispatch_leases" in call.args[0])
    assert update_call.args[2] == "completed"
    audit_insert.assert_awaited_once()
    assert audit_insert.await_args.kwargs["values"][4] == "completed"
