# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from scheduling import crud


@pytest.mark.anyio
async def test_update_scheduled_run_casts_reused_interval_parameter(monkeypatch):
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    monkeypatch.setattr(crud, "_get_pool", lambda: pool)

    await crud.update_scheduled_run(
        "sched-1",
        "org-1",
        name="Updated schedule",
        prompt="Updated prompt",
        interval_seconds=7200,
        enabled=True,
        callback_url="https://example.com/hook",
    )

    query = pool.fetchrow.await_args.args[0]
    assert "interval_seconds = $5" in query
    assert "next_run_at = NOW() + ($5::int * INTERVAL '1 second')" in query


@pytest.mark.anyio
async def test_create_scheduled_run_uses_first_run_at(monkeypatch):
    pool = AsyncMock()
    first_run_at = datetime.now(timezone.utc) + timedelta(hours=2)
    pool.fetchrow.return_value = {
        "id": "sched-1",
        "org_id": "org-1",
        "user_id": "user-1",
        "name": "Schedule",
        "prompt": "Run",
        "schedule_kind": "interval",
        "interval_seconds": 3600,
        "cron_expr": None,
        "enabled": True,
        "callback_url": None,
        "trigger_token": None,
        "next_run_at": first_run_at,
        "last_run_at": None,
        "consecutive_failures": 0,
        "created_at": first_run_at,
        "updated_at": first_run_at,
    }
    monkeypatch.setattr(crud, "_get_pool", lambda: pool)

    schedule = await crud.create_scheduled_run(
        org_id="org-1",
        user_id="user-1",
        name="Schedule",
        prompt="Run",
        interval_seconds=3600,
        callback_url=None,
        first_run_at=first_run_at,
    )

    assert schedule.next_run_at == first_run_at
    assert pool.fetchrow.await_args.args[-2] == first_run_at


@pytest.mark.anyio
async def test_create_scheduled_run_defaults_first_run_to_interval(monkeypatch):
    pool = AsyncMock()
    now = datetime.now(timezone.utc)
    pool.fetchrow.return_value = {
        "id": "sched-1",
        "org_id": "org-1",
        "user_id": "user-1",
        "name": "Schedule",
        "prompt": "Run",
        "schedule_kind": "interval",
        "interval_seconds": 3600,
        "cron_expr": None,
        "enabled": True,
        "callback_url": None,
        "trigger_token": None,
        "next_run_at": now,
        "last_run_at": None,
        "consecutive_failures": 0,
        "created_at": now,
        "updated_at": now,
    }
    monkeypatch.setattr(crud, "_get_pool", lambda: pool)

    await crud.create_scheduled_run(
        org_id="org-1",
        user_id="user-1",
        name="Schedule",
        prompt="Run",
        interval_seconds=3600,
        callback_url=None,
    )

    next_run_at = pool.fetchrow.await_args.args[-2]
    assert now + timedelta(seconds=3600) <= next_run_at <= datetime.now(timezone.utc) + timedelta(seconds=3600)


@pytest.mark.anyio
async def test_queue_scheduled_run_now_requires_enabled_interval(monkeypatch):
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    monkeypatch.setattr(crud, "_get_pool", lambda: pool)

    assert await crud.queue_scheduled_run_now("sched-1", "org-1") is None

    query = pool.fetchrow.await_args.args[0]
    assert "schedule_kind = 'interval'" in query
    assert "enabled = TRUE" in query
