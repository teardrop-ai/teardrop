from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.anyio
async def test_scheduled_runs_tick_executes_claimed_schedules(monkeypatch):
    schedules = [object(), object()]
    monkeypatch.setattr("scheduling.worker.claim_due_schedules", AsyncMock(return_value=schedules))
    execute_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("scheduling.worker.execute_scheduled_run", execute_mock)

    from scheduling.worker import scheduled_runs_tick

    await scheduled_runs_tick()

    assert execute_mock.await_count == 2


@pytest.mark.anyio
async def test_scheduled_runs_tick_isolates_failures(monkeypatch):
    """One execution raising must not abort the rest of the claimed batch."""
    schedules = [SimpleNamespace(id=f"sched-{i}") for i in range(4)]
    monkeypatch.setattr("scheduling.worker.claim_due_schedules", AsyncMock(return_value=schedules))

    executed: list[str] = []

    async def _execute(schedule):
        executed.append(schedule.id)
        if schedule.id == "sched-1":
            raise RuntimeError("boom")

    monkeypatch.setattr("scheduling.worker.execute_scheduled_run", AsyncMock(side_effect=_execute))

    from scheduling.worker import scheduled_runs_tick

    # Must not raise despite one execution failing.
    await scheduled_runs_tick()

    assert sorted(executed) == ["sched-0", "sched-1", "sched-2", "sched-3"]


@pytest.mark.anyio
async def test_scheduled_runs_tick_respects_concurrency_cap(monkeypatch):
    schedules = [SimpleNamespace(id=f"sched-{i}") for i in range(6)]
    monkeypatch.setattr("scheduling.worker.claim_due_schedules", AsyncMock(return_value=schedules))
    monkeypatch.setattr(
        "scheduling.worker.get_settings",
        lambda: SimpleNamespace(scheduled_runs_max_concurrency=2),
    )

    in_flight = 0
    max_in_flight = 0

    async def _execute(schedule):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1

    monkeypatch.setattr("scheduling.worker.execute_scheduled_run", AsyncMock(side_effect=_execute))

    from scheduling.worker import scheduled_runs_tick

    await scheduled_runs_tick()

    assert max_in_flight <= 2
