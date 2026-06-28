from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _schedule(schedule_id: str = "sched-1", org_id: str = "test-org-id") -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=schedule_id,
        org_id=org_id,
        user_id="test-user-id",
        name="Daily check",
        prompt="Summarize risk",
        schedule_kind="interval",
        interval_seconds=3600,
        enabled=True,
        callback_url="https://example.com/hook",
        next_run_at=now,
        last_run_at=None,
        consecutive_failures=0,
        created_at=now,
        updated_at=now,
    )


def _result() -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id="result-1",
        schedule_id="sched-1",
        org_id="test-org-id",
        run_id="run-1",
        status="completed",
        output_text="done",
        cost_usdc=12_345,
        error="",
        created_at=now,
    )


@pytest.mark.anyio
async def test_create_agent_schedule(api_client, test_settings, monkeypatch):
    test_settings.scheduled_runs_enabled = True
    monkeypatch.setattr("teardrop.routers.agent_schedules.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.agent_schedules.count_scheduled_runs", AsyncMock(return_value=0))
    create_mock = AsyncMock(return_value=_schedule())
    monkeypatch.setattr("teardrop.routers.agent_schedules.create_scheduled_run", create_mock)

    resp = await api_client.post(
        "/agent/schedules",
        json={
            "name": "Daily check",
            "prompt": "Summarize risk",
            "interval_seconds": 3600,
            "callback_url": "https://example.com/hook",
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "sched-1"
    create_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_create_agent_schedule_rejects_limit(api_client, test_settings, monkeypatch):
    test_settings.scheduled_runs_enabled = True
    test_settings.scheduled_runs_max_per_org = 1
    monkeypatch.setattr("teardrop.routers.agent_schedules.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.agent_schedules.count_scheduled_runs", AsyncMock(return_value=1))

    resp = await api_client.post(
        "/agent/schedules",
        json={"name": "Daily check", "prompt": "Summarize risk", "interval_seconds": 3600},
    )

    assert resp.status_code == 422
    assert "limit reached" in resp.json()["detail"]


@pytest.mark.anyio
async def test_create_agent_schedule_rejects_unsafe_callback(api_client, test_settings, monkeypatch):
    test_settings.scheduled_runs_enabled = True
    monkeypatch.setattr("teardrop.routers.agent_schedules.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.agent_schedules.count_scheduled_runs", AsyncMock(return_value=0))

    resp = await api_client.post(
        "/agent/schedules",
        json={
            "name": "Daily check",
            "prompt": "Summarize risk",
            "interval_seconds": 3600,
            "callback_url": "https://169.254.169.254/hook",
        },
    )

    assert resp.status_code == 422
    assert "Unsafe callback_url" in resp.json()["detail"]


@pytest.mark.anyio
async def test_update_agent_schedule_only_passes_set_fields(api_client, test_settings, monkeypatch):
    test_settings.scheduled_runs_enabled = True
    monkeypatch.setattr("teardrop.routers.agent_schedules.settings", test_settings)
    update_mock = AsyncMock(return_value=_schedule())
    monkeypatch.setattr("teardrop.routers.agent_schedules.update_scheduled_run", update_mock)

    resp = await api_client.patch("/agent/schedules/sched-1", json={"enabled": False})

    assert resp.status_code == 200
    kwargs = update_mock.await_args.kwargs
    assert kwargs == {"enabled": False}


@pytest.mark.anyio
async def test_get_agent_schedule_cross_org_404(api_client, test_settings, monkeypatch):
    test_settings.scheduled_runs_enabled = True
    monkeypatch.setattr("teardrop.routers.agent_schedules.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.agent_schedules.get_scheduled_run", AsyncMock(return_value=None))

    resp = await api_client.get("/agent/schedules/other-org-schedule")

    assert resp.status_code == 404


@pytest.mark.anyio
async def test_list_agent_schedule_runs(api_client, test_settings, monkeypatch):
    test_settings.scheduled_runs_enabled = True
    monkeypatch.setattr("teardrop.routers.agent_schedules.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.agent_schedules.get_scheduled_run", AsyncMock(return_value=_schedule()))
    monkeypatch.setattr(
        "teardrop.routers.agent_schedules.list_scheduled_run_results",
        AsyncMock(return_value=[_result()]),
    )

    resp = await api_client.get("/agent/schedules/sched-1/runs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["run_id"] == "run-1"


@pytest.mark.anyio
async def test_agent_schedules_disabled_returns_404(api_client, test_settings, monkeypatch):
    test_settings.scheduled_runs_enabled = False
    monkeypatch.setattr("teardrop.routers.agent_schedules.settings", test_settings)

    resp = await api_client.get("/agent/schedules")

    assert resp.status_code == 404
