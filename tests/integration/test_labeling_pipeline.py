# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from labeling.contracts import Observation, ObservationRequest, ScoreResult, TargetDraft
from labeling.store import (
    claim_due_targets,
    close_labeling_db,
    get_definition,
    init_labeling_db,
    insert_prediction,
    store_observation,
)
from migrations.runner import apply_pending
from shared.db_pool import create_pool


@pytest.fixture
async def labeling_db_pool(docker_postgres: str):
    pool = await create_pool(docker_postgres, min_size=1, max_size=5, name="integration-labeling")
    await apply_pending(pool)
    await init_labeling_db(pool)
    yield pool
    await close_labeling_db()
    await pool.close()


@pytest.mark.asyncio
async def test_labeling_prediction_claim_and_completion_are_idempotent(labeling_db_pool):
    definition = await get_definition("entry_timing", 1)
    assert definition is not None
    now = datetime.now(timezone.utc)
    target = TargetDraft(
        "token-1",
        {"id": "token-1", "signal": "ENTRY"},
        now - timedelta(minutes=2),
        now - timedelta(minutes=1),
        now - timedelta(seconds=1),
    )

    prediction_id, inserted = await insert_prediction(
        org_id="org-a",
        source_kind="scheduled_run",
        source_id="run-a",
        run_id="run-a",
        schedule_id="schedule-a",
        binding_id=None,
        definition=definition,
        predictions={"task_class": "entry_timing", "tokens": [target.item_payload]},
        targets=[target],
        prediction_at=now - timedelta(minutes=2),
    )
    assert inserted is True

    duplicate_id, duplicate_inserted = await insert_prediction(
        org_id="org-a",
        source_kind="scheduled_run",
        source_id="run-a",
        run_id="run-a",
        schedule_id="schedule-a",
        binding_id=None,
        definition=definition,
        predictions={"task_class": "entry_timing", "tokens": [target.item_payload]},
        targets=[target],
        prediction_at=now - timedelta(minutes=2),
    )
    assert duplicate_id == prediction_id
    assert duplicate_inserted is False

    claimed = await claim_due_targets(limit=10, max_per_org=1, lease_seconds=60)
    assert len(claimed) == 1
    request = ObservationRequest("token_price", "1", {"token": "token-1"}, target.window_end)
    observation_id = await store_observation(Observation(request, {"price": 101}))
    from labeling.store import complete_target

    completed = await complete_target(
        target_id=str(claimed[0]["id"]),
        lease_token=str(claimed[0]["lease_token"]),
        result=ScoreResult(label="up", score=1, status="correct", actual={"price": 101}),
        observation_id=observation_id,
    )
    assert completed is True
    assert (
        await complete_target(
            target_id=str(claimed[0]["id"]),
            lease_token=str(claimed[0]["lease_token"]),
            result=ScoreResult(label="up", score=1, status="correct", actual={"price": 101}),
            observation_id=observation_id,
        )
        is False
    )
    assert await labeling_db_pool.fetchval("SELECT status FROM labeling_targets WHERE id = $1", claimed[0]["id"]) == "scored"
