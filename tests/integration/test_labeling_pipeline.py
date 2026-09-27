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
    await pool.execute("TRUNCATE TABLE labeling_predictions, labeling_observations, commitment_batches RESTART IDENTITY CASCADE")
    yield pool
    await pool.execute("TRUNCATE TABLE labeling_predictions, labeling_observations, commitment_batches RESTART IDENTITY CASCADE")
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


@pytest.mark.asyncio
async def test_committed_prediction_is_sealed_immutable_and_retained(labeling_db_pool):
    from labeling.anchor import batch_leaves, seal_batch
    from labeling.commitments import audit_path, merkle_root, verify_path
    from shared.db_pool import CheckViolation
    from teardrop.retention import _DELETE_LABELING_PREDICTIONS_SQL

    definition = await get_definition("entry_timing", 1)
    assert definition is not None
    now = datetime.now(timezone.utc)
    prediction_id, _ = await insert_prediction(
        org_id="org-a",
        source_kind="external",
        source_id="key-a",
        run_id="",
        schedule_id="",
        binding_id=None,
        definition=definition,
        predictions={"task_class": "entry_timing", "tokens": [{"id": "token-1"}]},
        targets=[],
        prediction_at=now - timedelta(days=400),
        signer_address="0x" + "ab" * 20,
        signature="0x" + "cd" * 65,
        commit=True,
    )

    batch_id = await seal_batch(84532)
    assert batch_id is not None
    assert await seal_batch(84532) is None
    row = await labeling_db_pool.fetchrow(
        """
        SELECT p.leaf_sha256, p.anchor_leaf_index, b.merkle_root, b.leaf_count
        FROM labeling_predictions p JOIN commitment_batches b ON b.id = p.anchor_batch_id
        WHERE p.id = $1
        """,
        prediction_id,
    )
    leaves = await batch_leaves(batch_id, row["leaf_count"])
    assert merkle_root(leaves) == row["merkle_root"]
    index = row["anchor_leaf_index"]
    assert verify_path(row["leaf_sha256"], index, len(leaves), audit_path(leaves, index), row["merkle_root"])

    with pytest.raises(CheckViolation):
        await labeling_db_pool.execute(
            "UPDATE labeling_predictions SET payload_sha256 = $2 WHERE id = $1", prediction_id, "0" * 64
        )
    with pytest.raises(CheckViolation):
        await labeling_db_pool.execute("UPDATE labeling_predictions SET anchor_leaf_index = 99 WHERE id = $1", prediction_id)
    with pytest.raises(CheckViolation):
        await labeling_db_pool.execute("DELETE FROM labeling_predictions WHERE id = $1", prediction_id)
    with pytest.raises(CheckViolation):
        await labeling_db_pool.execute("UPDATE commitment_batches SET merkle_root = $2 WHERE id = $1", batch_id, "0" * 64)

    assert await labeling_db_pool.fetchval(_DELETE_LABELING_PREDICTIONS_SQL, 1, 100) == 0
    assert await labeling_db_pool.fetchval("SELECT COUNT(*) FROM labeling_predictions WHERE id = $1", prediction_id) == 1


@pytest.mark.asyncio
async def test_external_prediction_requires_signature_and_leaf(labeling_db_pool):
    from shared.db_pool import CheckViolation

    definition = await get_definition("entry_timing", 1)
    assert definition is not None
    with pytest.raises(CheckViolation):
        await insert_prediction(
            org_id="org-a",
            source_kind="external",
            source_id="key-unsigned",
            run_id="",
            schedule_id="",
            binding_id=None,
            definition=definition,
            predictions={"task_class": "entry_timing", "tokens": [{"id": "token-1"}]},
            targets=[],
            prediction_at=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_labeling_definitions_are_immutable_except_active(labeling_db_pool):
    from shared.db_pool import CheckViolation

    assert await get_definition("oracle_deviation", 1) is not None
    with pytest.raises(CheckViolation):
        await labeling_db_pool.execute(
            "UPDATE labeling_definitions SET config = '{}'::jsonb WHERE definition_key = 'oracle_deviation'"
        )
    with pytest.raises(CheckViolation):
        await labeling_db_pool.execute("DELETE FROM labeling_definitions WHERE definition_key = 'oracle_deviation'")
    await labeling_db_pool.execute("UPDATE labeling_definitions SET active = FALSE WHERE definition_key = 'oracle_deviation'")
    await labeling_db_pool.execute("UPDATE labeling_definitions SET active = TRUE WHERE definition_key = 'oracle_deviation'")


async def _commit_oracle_prediction(definition, *, signer: str, key: str, prediction_at: datetime, anchored_at: datetime):
    from labeling.anchor import seal_batch
    from labeling.onchain import parse_oracle_rounds

    predictions = {"deviation_bps": {"lt25": 0.6, "b25_50": 0.1, "b50_100": 0.1, "b100_200": 0.1, "gt200": 0.1}}
    prediction_id, _ = await insert_prediction(
        org_id="org-a",
        source_kind="external",
        source_id=key,
        run_id="",
        schedule_id="",
        binding_id=None,
        definition=definition,
        predictions=predictions,
        targets=parse_oracle_rounds(predictions, definition, prediction_at),
        prediction_at=prediction_at,
        signer_address=signer,
        signature="0x" + "cd" * 65,
        commit=True,
    )
    batch_id = await seal_batch(8453)
    await _confirm_batch(batch_id, anchored_at)
    return prediction_id


async def _confirm_batch(batch_id: str, anchored_at: datetime) -> None:
    from labeling.store import _get_pool

    await _get_pool().execute(
        """
        UPDATE commitment_batches
        SET tx_hash = $2, anchor_address = $3, block_number = 1, anchored_at = $4
        WHERE id = $1
        """,
        batch_id,
        "0x" + "ef" * 32,
        "0x" + "12" * 20,
        anchored_at,
    )


@pytest.mark.asyncio
async def test_scorecard_rows_apply_public_eligibility(labeling_db_pool):
    from labeling.scorecards import _ROWS_SQL, get_public_definition

    definition = await get_public_definition("oracle_deviation", 1)
    assert definition is not None
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    predicted_at = today - timedelta(days=4) + timedelta(hours=6)
    early, late = "0x" + "aa" * 20, "0x" + "bb" * 20

    first = await _commit_oracle_prediction(
        definition, signer=early, key="k1", prediction_at=predicted_at, anchored_at=predicted_at + timedelta(hours=1)
    )
    await _commit_oracle_prediction(
        definition,
        signer=early,
        key="k2",
        prediction_at=predicted_at + timedelta(minutes=5),
        anchored_at=predicted_at + timedelta(hours=1),
    )
    await _commit_oracle_prediction(
        definition, signer=late, key="k3", prediction_at=predicted_at, anchored_at=predicted_at + timedelta(days=3)
    )

    target_id = await labeling_db_pool.fetchval("SELECT id FROM labeling_targets WHERE prediction_id = $1", first)
    window_end = await labeling_db_pool.fetchval("SELECT window_end FROM labeling_targets WHERE id = $1", target_id)
    observation_id = await store_observation(
        Observation(ObservationRequest("oracle_deviation", "1", {"start": "s"}, window_end), {"start": {}, "end": {}})
    )
    await labeling_db_pool.execute(
        """
        INSERT INTO labeling_results
            (id, target_id, scorer_key, scorer_version, observation_id, label, score, status, source, created_at)
        VALUES ('r-auto', $1, 'oracle_deviation_brier', '1', $2, 'lt25', 0.2, 'correct', 'automatic', NOW()),
               ('r-manual', $1, 'oracle_deviation_brier', '1', NULL, 'gt200', 1.8, 'incorrect', 'manual',
                NOW() - INTERVAL '1 hour')
        """,
        target_id,
        observation_id,
    )

    rows = await labeling_db_pool.fetch(_ROWS_SQL, "oracle_deviation", 1, predicted_at - timedelta(days=1))

    assert [(row["subject"], row["status"], float(row["score"])) for row in rows] == [(early, "correct", 0.2)]

    from labeling.store import append_result_override
    from shared.db_pool import CheckViolation

    for statement, record_id in (
        ("UPDATE labeling_targets SET item_payload = '{}'::jsonb WHERE id = $1", target_id),
        ("DELETE FROM labeling_targets WHERE id = $1", target_id),
        ("UPDATE labeling_results SET score = 0 WHERE id = $1", "r-auto"),
        ("DELETE FROM labeling_results WHERE id = $1", "r-auto"),
        ("UPDATE labeling_observations SET payload = '{}'::jsonb WHERE id = $1", observation_id),
        ("DELETE FROM labeling_observations WHERE id = $1", observation_id),
    ):
        with pytest.raises(CheckViolation):
            await labeling_db_pool.execute(statement, record_id)

    override = ScoreResult(label="gt200", score=1.8, status="incorrect", source="manual")
    assert not await append_result_override(target_id=target_id, org_id="org-a", result=override)
    await labeling_db_pool.execute("UPDATE labeling_targets SET status = 'scored' WHERE id = $1", target_id)
    assert not await append_result_override(target_id=target_id, org_id="org-other", result=override)
    assert await append_result_override(target_id=target_id, org_id="org-a", result=override)
    assert await labeling_db_pool.fetchval("SELECT status FROM labeling_targets WHERE id = $1", target_id) == "scored"


@pytest.mark.asyncio
async def test_scorecards_keep_inactive_subjects_and_reject_replacement_predictions(labeling_db_pool):
    from labeling.scorecards import _ROWS_SQL, get_public_definition, summarize

    definition = await get_public_definition("oracle_deviation", 1)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    inactive, replacement = "0x" + "33" * 20, "0x" + "44" * 20
    old = today - timedelta(days=100)
    await _commit_oracle_prediction(definition, signer=inactive, key="old", prediction_at=old, anchored_at=old)
    recent = today - timedelta(days=5)
    await _commit_oracle_prediction(definition, signer=replacement, key="late-first", prediction_at=recent, anchored_at=today)
    await _commit_oracle_prediction(
        definition,
        signer=replacement,
        key="early-second",
        prediction_at=recent + timedelta(hours=1),
        anchored_at=recent + timedelta(hours=2),
    )

    cutoff = today - timedelta(days=30)
    rows = await labeling_db_pool.fetch(_ROWS_SQL, definition.key, definition.version, cutoff)

    assert len(rows) == 1
    assert rows[0]["subject"] == inactive
    assert rows[0]["window_start"] is None
    (card,) = summarize(rows, definition.config, today, window_start=cutoff)
    assert card["rounds_expected"] > 0
    assert card["rounds_submitted"] == card["n_scored"] == 0
    assert card["eligible"] is False


@pytest.mark.asyncio
async def test_pinned_observation_conflicts_fail_closed_under_concurrency(labeling_db_pool):
    import asyncio

    request = ObservationRequest("oracle_deviation", "1", {"start": "round"}, datetime.now(timezone.utc))
    first, conflicting = Observation(request, {"block": 1}), Observation(request, {"block": 2})
    results = await asyncio.gather(
        store_observation(first, require_identical=True),
        store_observation(conflicting, require_identical=True),
        return_exceptions=True,
    )

    assert sum(isinstance(result, str) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    record = await labeling_db_pool.fetchrow("SELECT id, payload FROM labeling_observations")
    assert await store_observation(Observation(request, record["payload"]), require_identical=True) == record["id"]
    assert await labeling_db_pool.fetchval("SELECT COUNT(*) FROM labeling_observations") == 1
