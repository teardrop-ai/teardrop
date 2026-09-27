# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from labeling import worker


def _row():
    now = datetime.now(timezone.utc)
    return {
        "id": "target-1",
        "lease_token": "lease-1",
        "attempts": 5,
        "item_key": "root",
        "item_payload": {"value": 1},
        "window_start": now,
        "window_end": now + timedelta(days=1),
        "definition_key": "example",
        "definition_version": 1,
        "prediction_schema": {},
        "target_schema": {},
        "outcome_schema": {},
        "parser_key": "parser",
        "parser_version": "1",
        "provider_key": "provider",
        "provider_version": "1",
        "scorer_key": "scorer",
        "scorer_version": "1",
        "config": {},
    }


@pytest.mark.anyio
async def test_planning_failure_at_retry_limit_becomes_unavailable(monkeypatch):
    row = _row()
    provider = MagicMock()
    provider.plan.side_effect = ValueError("poison target")
    complete = AsyncMock(return_value=True)
    retry = AsyncMock()
    monkeypatch.setattr(worker, "resolve_provider", lambda *_: provider)
    monkeypatch.setattr(worker, "complete_target", complete)
    monkeypatch.setattr(worker, "retry_target", retry)

    assert await worker._process_claimed_rows([row]) == 0

    retry.assert_not_awaited()
    complete.assert_awaited_once()
    result = complete.await_args.kwargs["result"]
    assert result.status == "unavailable"
    assert result.label == "unavailable"


@pytest.mark.anyio
@pytest.mark.parametrize("attempts", [5, 12])
@pytest.mark.parametrize("raises", [False, True])
async def test_public_observation_failure_remains_retryable_and_redacted(monkeypatch, caplog, attempts, raises):
    from labeling.contracts import Observation, ObservationRequest, ScoreResult

    row = {**_row(), "attempts": attempts, "config": {"public": True}}
    request = ObservationRequest("provider", "1", {}, row["window_end"])
    provider = MagicMock()
    provider.plan.return_value = request
    provider.fetch_batch = AsyncMock(return_value={})
    if raises:
        provider.fetch_batch.side_effect = OSError("https://rpc.example/SECRET-KEY")
    complete = AsyncMock(return_value=True)
    retry = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "resolve_provider", lambda *_: provider)
    monkeypatch.setattr(worker, "complete_target", complete)
    monkeypatch.setattr(worker, "retry_target", retry)

    assert await worker._process_claimed_rows([row]) == 0

    complete.assert_not_awaited()
    retry.assert_awaited_once_with("target-1", "lease-1", "observation unavailable", worker._retry_delay(attempts))
    assert "SECRET-KEY" not in caplog.text

    provider.fetch_batch.side_effect = None
    provider.fetch_batch.return_value = {request.request_sha256: Observation(request, {})}
    monkeypatch.setattr(worker, "store_observation", AsyncMock(return_value="observation-1"))
    monkeypatch.setattr(worker, "resolve_scorer", lambda *_: lambda *_: ScoreResult(label="lt25", status="correct", score=0.2))

    assert await worker._process_claimed_rows([row]) == 1
    assert complete.await_args.kwargs["observation_id"] == "observation-1"
