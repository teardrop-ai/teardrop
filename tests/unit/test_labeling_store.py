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


def _commit_connection(monkeypatch, fetchrow_side_effect):
    connection = MagicMock()
    connection.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
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
    return connection


async def _insert_external(*, predictions=None, commit=True, parse_error=""):
    return await store.insert_prediction(
        org_id="org-1",
        source_kind="external",
        source_id="key-1",
        run_id="",
        schedule_id="",
        binding_id=None,
        definition=Definition(key="example", version=1),
        predictions=predictions or {"value": 1},
        targets=[],
        prediction_at=datetime(2026, 9, 26, 12, 0, 0, 123456, tzinfo=timezone.utc),
        parse_error=parse_error,
        signer_address="0x" + "ab" * 20,
        signature="0x" + "cd" * 65,
        commit=commit,
    )


@pytest.mark.anyio
async def test_committed_insert_writes_verifiable_leaf(monkeypatch):
    from labeling.commitments import LEAF_VERSION_PREDICTION, leaf_hash, prediction_leaf_fields

    connection = _commit_connection(monkeypatch, [{"id": "ignored"}])

    prediction_id, inserted = await _insert_external()

    assert inserted is True
    args = connection.fetchrow.await_args_list[0].args
    assert "leaf_sha256" in args[0]
    signer, signature, salt, leaf = args[15:19]
    assert (signer, signature) == ("0x" + "ab" * 20, "0x" + "cd" * 65)
    fields = prediction_leaf_fields(
        prediction_id=args[1],
        org_id="org-1",
        signer_address=signer,
        definition_key="example",
        definition_version=1,
        payload_sha256=args[11],
        prediction_at=args[12],
    )
    assert leaf == leaf_hash(LEAF_VERSION_PREDICTION, fields, salt)


@pytest.mark.anyio
@pytest.mark.parametrize(("commit", "parse_error"), [(False, ""), (True, "invalid")])
async def test_uncommitted_or_invalid_insert_has_no_leaf(monkeypatch, commit, parse_error):
    connection = _commit_connection(monkeypatch, [{"id": "ignored"}])

    await _insert_external(commit=commit, parse_error=parse_error)

    assert connection.fetchrow.await_args_list[0].args[17:19] == (None, None)


@pytest.mark.anyio
async def test_external_replay_with_different_payload_conflicts(monkeypatch):
    _commit_connection(monkeypatch, [None, {"id": "prediction-1", "status": "accepted", "payload_sha256": "0" * 64}])

    with pytest.raises(store.PredictionConflictError):
        await _insert_external()


@pytest.mark.anyio
async def test_external_replay_with_same_payload_is_idempotent(monkeypatch):
    from labeling.contracts import validate_prediction

    same_hash = validate_prediction({"value": 1})
    _commit_connection(monkeypatch, [None, {"id": "prediction-1", "status": "accepted", "payload_sha256": same_hash}])

    assert await _insert_external() == ("prediction-1", False)


def _override_connection(monkeypatch, *, status: str, committed: bool):
    row = {"id": "target-1", "status": status, "scorer_key": "s", "scorer_version": "1", "committed": committed}
    connection = _commit_connection(monkeypatch, [row])
    connection.execute = AsyncMock()
    return connection


async def _override():
    from labeling.contracts import ScoreResult

    return await store.append_result_override(
        target_id="target-1",
        org_id="org-1",
        result=ScoreResult(label="up", score=1, status="correct", source="manual"),
    )


@pytest.mark.anyio
async def test_override_cannot_preempt_automatic_scoring_of_committed_target(monkeypatch):
    connection = _override_connection(monkeypatch, status="pending", committed=True)

    assert await _override() is False
    connection.execute.assert_not_awaited()


@pytest.mark.anyio
async def test_override_of_scored_committed_target_only_appends(monkeypatch):
    connection = _override_connection(monkeypatch, status="scored", committed=True)

    assert await _override() is True
    assert connection.execute.await_count == 1
    assert "INSERT INTO labeling_results" in connection.execute.await_args.args[0]


@pytest.mark.anyio
async def test_override_of_uncommitted_target_updates_status(monkeypatch):
    connection = _override_connection(monkeypatch, status="pending", committed=False)

    assert await _override() is True
    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert "UPDATE labeling_targets" in statements[0]
    assert "INSERT INTO labeling_results" in statements[1]
