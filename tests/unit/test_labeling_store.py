# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from labeling import store
from labeling.contracts import Definition, TargetDraft


@pytest.mark.anyio
async def test_insert_prediction_conflict_lookup_is_org_scoped(monkeypatch):
    connection = MagicMock()
    connection.fetchrow = AsyncMock(side_effect=[None, {"id": "prediction-1", "status": "accepted"}])
    connection.executemany = AsyncMock()

    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    connection.transaction.return_value = transaction

    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=connection)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    monkeypatch.setattr(store, "_pool", pool)

    now = datetime.now(timezone.utc)
    prediction_id, inserted = await store.insert_prediction(
        org_id="org-1",
        source_kind="scheduled_run",
        source_id="run-1",
        run_id="run-1",
        schedule_id="schedule-1",
        binding_id=None,
        definition=Definition(key="example", version=1),
        predictions={"task_class": "example"},
        targets=[],
        prediction_at=now,
    )

    assert prediction_id == "prediction-1"
    assert inserted is False
    conflict_lookup = connection.fetchrow.await_args_list[1]
    assert conflict_lookup.args[1:] == ("org-1", "scheduled_run", "run-1", "example", 1)
    assert "WHERE org_id = $1" in conflict_lookup.args[0]


@pytest.mark.anyio
async def test_insert_prediction_does_not_expand_existing_invalid_prediction(monkeypatch):
    connection = MagicMock()
    connection.fetchrow = AsyncMock(side_effect=[None, {"id": "prediction-1", "status": "invalid"}])
    connection.executemany = AsyncMock()

    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    connection.transaction.return_value = transaction

    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=connection)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    monkeypatch.setattr(store, "_pool", pool)

    now = datetime.now(timezone.utc)
    target = TargetDraft(
        "root",
        {"value": 1},
        now,
        now + timedelta(days=1),
        now + timedelta(days=1),
    )
    prediction_id, inserted = await store.insert_prediction(
        org_id="org-1",
        source_kind="scheduled_run",
        source_id="run-1",
        run_id="run-1",
        schedule_id="schedule-1",
        binding_id=None,
        definition=Definition(key="example", version=1),
        predictions={"task_class": "example"},
        targets=[target],
        prediction_at=now,
    )

    assert prediction_id == "prediction-1"
    assert inserted is False
    connection.executemany.assert_not_awaited()
