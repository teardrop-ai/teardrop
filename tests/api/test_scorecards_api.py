# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from labeling.contracts import Definition

_DEFINITION = Definition(
    key="oracle_deviation",
    version=1,
    prediction_schema={"type": "object"},
    config={"public": True, "min_sample": 30, "round_seconds": 86400, "horizon_seconds": 86400, "finality_seconds": 1800},
)
_SIGNER = "0x" + "ab" * 20


def _card(subject: str, *, eligible: bool) -> dict:
    return {
        "subject": subject,
        "platform_attested": subject.startswith("schedule:"),
        "eligible": eligible,
        "n_scored": 40 if eligible else 3,
        "rounds_submitted": 40 if eligible else 3,
        "rounds_expected": 42,
        "coverage": 0.952381 if eligible else None,
        "unresolved": 0,
        "mean_brier": 0.21 if eligible else None,
        "adjusted_brier": 0.24 if eligible else None,
        "accuracy": 0.6 if eligible else None,
    }


@pytest.fixture
def scorecard_env(monkeypatch):
    settings = SimpleNamespace(vor_enabled=True, trusted_proxy_count=0, rate_limit_auth_rpm=1000)
    monkeypatch.setattr("teardrop.routers.scorecards.get_settings", lambda: settings)
    limiter = AsyncMock()
    monkeypatch.setattr("teardrop.routers.scorecards._enforce_rate_limit", limiter)
    lookup = AsyncMock(return_value=_DEFINITION)
    monkeypatch.setattr("teardrop.routers.scorecards.get_public_definition", lookup)
    compute = AsyncMock(return_value=[_card(_SIGNER, eligible=True), _card("schedule:new", eligible=False)])
    monkeypatch.setattr("teardrop.routers.scorecards.compute_scorecards", compute)
    monkeypatch.setattr("teardrop.routers.scorecards.list_public_definitions", AsyncMock(return_value=[_DEFINITION]))
    return SimpleNamespace(settings=settings, limiter=limiter, lookup=lookup, compute=compute)


@pytest.mark.anyio
async def test_tasks_are_public_and_hashed(anon_client, scorecard_env):
    from labeling.scorecards import definition_sha256

    response = await anon_client.get("/scorecards/tasks")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    item = response.json()["items"][0]
    assert item["definition_sha256"] == definition_sha256(_DEFINITION)
    assert scorecard_env.limiter.await_args.args[0].startswith("scorecards:")


@pytest.mark.anyio
async def test_leaderboard_lists_only_eligible_subjects(anon_client, scorecard_env):
    response = await anon_client.get("/scorecards/oracle_deviation/1?window_days=30")

    assert response.status_code == 200
    body = response.json()
    assert [item["subject"] for item in body["items"]] == [_SIGNER]
    assert (body["window_days"], body["min_sample"]) == (30, 30)
    scorecard_env.compute.assert_awaited_once_with(_DEFINITION, 30)


@pytest.mark.anyio
async def test_subject_scorecard_below_min_sample_hides_metrics(anon_client, scorecard_env):
    scorecard_env.compute.return_value = [_card("schedule:new", eligible=False)]

    response = await anon_client.get("/scorecards/oracle_deviation/1/schedule:new")

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["eligible"] is False
    assert item["mean_brier"] is None
    scorecard_env.compute.assert_awaited_once_with(_DEFINITION, 90, "schedule:new")


@pytest.mark.anyio
async def test_subject_scorecard_not_found(anon_client, scorecard_env):
    scorecard_env.compute.return_value = []

    response = await anon_client.get(f"/scorecards/oracle_deviation/1/{_SIGNER}")

    assert response.status_code == 404


@pytest.mark.anyio
async def test_private_or_unknown_task_returns_404(anon_client, scorecard_env):
    scorecard_env.lookup.return_value = None

    response = await anon_client.get("/scorecards/entry_timing/1")

    assert response.status_code == 404
    scorecard_env.compute.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "/scorecards/oracle_deviation/1?window_days=7",
        "/scorecards/Oracle/1",
        "/scorecards/oracle_deviation/0",
        f"/scorecards/oracle_deviation/1/{_SIGNER.upper()}",
        "/scorecards/oracle_deviation/1/schedule:bad%20id",
    ],
)
async def test_invalid_parameters_are_rejected(anon_client, scorecard_env, path):
    response = await anon_client.get(path)

    assert response.status_code == 422
    scorecard_env.compute.assert_not_awaited()


@pytest.mark.anyio
async def test_scorecards_disabled_returns_404(anon_client, scorecard_env):
    scorecard_env.settings.vor_enabled = False

    response = await anon_client.get("/scorecards/tasks")

    assert response.status_code == 404
    scorecard_env.limiter.assert_not_awaited()
