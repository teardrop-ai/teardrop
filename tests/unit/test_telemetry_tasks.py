# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import asyncio

import pytest

from teardrop.telemetry_tasks import close_telemetry_tasks, init_telemetry_tasks, schedule_telemetry_task


@pytest.fixture(autouse=True)
def reset_telemetry_tasks():
    yield
    init_telemetry_tasks(32)


@pytest.mark.asyncio
async def test_registry_drops_work_at_capacity_and_recovers_after_completion():
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    dropped_ran = False

    async def blocking_task():
        started.set()
        await release.wait()
        completed.set()

    async def dropped_task():
        nonlocal dropped_ran
        dropped_ran = True

    init_telemetry_tasks(1)
    assert schedule_telemetry_task(blocking_task(), name="blocking")
    await started.wait()
    assert not schedule_telemetry_task(dropped_task(), name="dropped")
    assert dropped_ran is False

    release.set()
    await completed.wait()
    await asyncio.sleep(0)
    assert schedule_telemetry_task(dropped_task(), name="replacement")
    await close_telemetry_tasks()


@pytest.mark.asyncio
async def test_close_cancels_tracked_tasks():
    cancelled = asyncio.Event()

    async def blocking_task():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    init_telemetry_tasks(1)
    assert schedule_telemetry_task(blocking_task(), name="blocking")
    await asyncio.sleep(0)
    await close_telemetry_tasks()
    assert cancelled.is_set()
