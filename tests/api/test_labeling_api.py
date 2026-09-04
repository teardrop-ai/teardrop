# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


@pytest.mark.anyio
async def test_list_labeling_predictions_is_org_scoped(api_client, monkeypatch):
    rows = [
        {
            "id": "prediction-1",
            "org_id": "test-org-id",
            "source_kind": "scheduled_run",
            "source_id": "run-1",
            "run_id": "run-1",
            "schedule_id": "schedule-1",
            "definition_key": "entry_timing",
            "definition_version": 1,
            "predictions": {"task_class": "entry_timing"},
            "payload_sha256": "a" * 64,
            "prediction_at": _NOW,
            "status": "accepted",
            "parse_error": "",
            "created_at": _NOW,
        }
    ]
    monkeypatch.setattr("teardrop.routers.labeling.list_predictions", AsyncMock(return_value=rows))

    response = await api_client.get("/labeling/predictions")

    assert response.status_code == 200
    assert response.json()["items"][0]["predictions"]["task_class"] == "entry_timing"


@pytest.mark.anyio
async def test_bind_labeling_definition_requires_owned_schedule(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.labeling.get_scheduled_run", AsyncMock(return_value=None))

    response = await api_client.post(
        "/labeling/bindings",
        json={"schedule_id": "schedule-1", "definition_key": "entry_timing", "definition_version": 1},
    )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_bind_labeling_definition(api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.labeling.get_scheduled_run",
        AsyncMock(return_value=SimpleNamespace(id="schedule-1", org_id="test-org-id")),
    )
    monkeypatch.setattr(
        "teardrop.routers.labeling.get_definition",
        AsyncMock(return_value=SimpleNamespace(key="entry_timing", version=1)),
    )
    binding = AsyncMock(return_value="binding-1")
    monkeypatch.setattr("teardrop.routers.labeling.create_binding", binding)

    response = await api_client.post(
        "/labeling/bindings",
        json={"schedule_id": "schedule-1", "definition_key": "entry_timing", "definition_version": 1},
    )

    assert response.status_code == 201
    assert response.json()["id"] == "binding-1"
    binding.assert_awaited_once()


@pytest.mark.anyio
async def test_override_labeling_result_appends_external_result(api_client, monkeypatch):
    override = AsyncMock(return_value=True)
    monkeypatch.setattr("teardrop.routers.labeling.append_result_override", override)

    response = await api_client.post(
        "/labeling/results/target-1/override",
        json={
            "label": "up",
            "score": 1,
            "status": "correct",
            "actual": {"direction": "up"},
            "source": "external",
        },
    )

    assert response.status_code == 201
    assert response.json() == {"status": "recorded"}
    override.assert_awaited_once()


@pytest.mark.anyio
async def test_override_labeling_result_rejects_automatic_source(api_client, monkeypatch):
    response = await api_client.post(
        "/labeling/results/target-1/override",
        json={"label": "up", "score": 1, "status": "correct", "source": "automatic"},
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_labeling_requires_auth(anon_client):
    response = await anon_client.get("/labeling/results")
    assert response.status_code == 401
