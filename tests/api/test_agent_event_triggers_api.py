from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_SECRET = "test-secret-value"
_SECRET_HASH = hashlib.sha256(_SECRET.encode("utf-8")).hexdigest()


def _event_schedule(enabled: bool = True, trigger_token: str = "tok-abc") -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id="evt-1",
        org_id="test-org-id",
        user_id="test-user-id",
        name="On payment",
        prompt="Handle {{kind}}",
        schedule_kind="event",
        interval_seconds=None,
        enabled=enabled,
        callback_url=None,
        trigger_token=trigger_token,
        next_run_at=None,
        last_run_at=None,
        consecutive_failures=0,
        created_at=now,
        updated_at=now,
    )


def _enable(monkeypatch, test_settings):
    test_settings.event_triggers_enabled = True
    monkeypatch.setattr("teardrop.routers.agent_event_triggers.settings", test_settings)


# ── Management CRUD ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_event_trigger_returns_secret_once(api_client, test_settings, monkeypatch):
    _enable(monkeypatch, test_settings)
    monkeypatch.setattr("teardrop.routers.agent_event_triggers.count_scheduled_runs", AsyncMock(return_value=0))
    create_mock = AsyncMock(return_value=_event_schedule())
    monkeypatch.setattr("teardrop.routers.agent_event_triggers.create_event_trigger", create_mock)

    resp = await api_client.post(
        "/agent/event-triggers",
        json={"name": "On payment", "prompt": "Handle {{kind}}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["trigger_token"] == "tok-abc"
    assert body["event_path"] == "/agent/events/tok-abc"
    assert body["secret"]  # plaintext returned exactly once
    assert "secret_hash" not in body
    create_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_create_event_trigger_rejects_limit(api_client, test_settings, monkeypatch):
    _enable(monkeypatch, test_settings)
    test_settings.event_triggers_max_per_org = 1
    monkeypatch.setattr("teardrop.routers.agent_event_triggers.count_scheduled_runs", AsyncMock(return_value=1))

    resp = await api_client.post("/agent/event-triggers", json={"name": "x", "prompt": "y"})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_list_event_triggers_excludes_secret(api_client, test_settings, monkeypatch):
    _enable(monkeypatch, test_settings)
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.list_event_triggers",
        AsyncMock(return_value=[_event_schedule()]),
    )

    resp = await api_client.get("/agent/event-triggers")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert "secret" not in items[0]
    assert "secret_hash" not in items[0]


@pytest.mark.anyio
async def test_rotate_secret_returns_new_secret(api_client, test_settings, monkeypatch):
    _enable(monkeypatch, test_settings)
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.get_scheduled_run",
        AsyncMock(return_value=_event_schedule()),
    )
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.rotate_event_trigger_secret",
        AsyncMock(return_value=True),
    )

    resp = await api_client.post("/agent/event-triggers/evt-1/rotate-secret")
    assert resp.status_code == 200
    assert resp.json()["secret"]


@pytest.mark.anyio
async def test_create_event_trigger_disabled_returns_404(api_client, test_settings, monkeypatch):
    test_settings.event_triggers_enabled = False
    monkeypatch.setattr("teardrop.routers.agent_event_triggers.settings", test_settings)
    resp = await api_client.post("/agent/event-triggers", json={"name": "x", "prompt": "y"})
    assert resp.status_code == 404


# ── Public inbound dispatch ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_dispatch_unknown_token_returns_404(anon_client, test_settings, monkeypatch):
    _enable(monkeypatch, test_settings)
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.get_event_trigger_for_dispatch",
        AsyncMock(return_value=None),
    )
    resp = await anon_client.post(
        "/agent/events/nope",
        json={"kind": "payment"},
        headers={"X-Teardrop-Trigger-Secret": _SECRET},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_dispatch_bad_secret_returns_401(anon_client, test_settings, monkeypatch):
    _enable(monkeypatch, test_settings)
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.get_event_trigger_for_dispatch",
        AsyncMock(return_value=(_event_schedule(), _SECRET_HASH)),
    )
    resp = await anon_client.post(
        "/agent/events/tok-abc",
        json={"kind": "payment"},
        headers={"X-Teardrop-Trigger-Secret": "wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_dispatch_disabled_trigger_returns_404(anon_client, test_settings, monkeypatch):
    _enable(monkeypatch, test_settings)
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.get_event_trigger_for_dispatch",
        AsyncMock(return_value=(_event_schedule(enabled=False), _SECRET_HASH)),
    )
    resp = await anon_client.post(
        "/agent/events/tok-abc",
        json={"kind": "payment"},
        headers={"X-Teardrop-Trigger-Secret": _SECRET},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_dispatch_accepts_and_runs_in_background(anon_client, test_settings, monkeypatch):
    _enable(monkeypatch, test_settings)
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.get_event_trigger_for_dispatch",
        AsyncMock(return_value=(_event_schedule(), _SECRET_HASH)),
    )
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.get_existing_dispatch",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.reserve_event_dispatch",
        AsyncMock(return_value=("evt-run-id", True)),
    )
    exec_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.agent_event_triggers.execute_event_run", exec_mock)

    resp = await anon_client.post(
        "/agent/events/tok-abc",
        json={"kind": "payment"},
        headers={"X-Teardrop-Trigger-Secret": _SECRET, "X-Idempotency-Key": "evt-key-1"},
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["run_id"]
    # Let the background task run, then assert it executed with the rendered prompt.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    exec_mock.assert_awaited_once()
    assert exec_mock.await_args.kwargs["prompt"] == "Handle payment"
    assert exec_mock.await_args.kwargs["run_id"] == "evt-run-id"


@pytest.mark.anyio
async def test_dispatch_duplicate_idempotency_key_skips(anon_client, test_settings, monkeypatch):
    _enable(monkeypatch, test_settings)
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.get_event_trigger_for_dispatch",
        AsyncMock(return_value=(_event_schedule(), _SECRET_HASH)),
    )
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.get_existing_dispatch",
        AsyncMock(return_value="prior-run-id"),
    )
    exec_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.agent_event_triggers.execute_event_run", exec_mock)

    resp = await anon_client.post(
        "/agent/events/tok-abc",
        json={"kind": "payment"},
        headers={"X-Teardrop-Trigger-Secret": _SECRET, "X-Idempotency-Key": "evt-key-1"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "duplicate"
    assert body["run_id"] == "prior-run-id"
    await asyncio.sleep(0)
    exec_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_dispatch_backpressure_returns_429(anon_client, test_settings, monkeypatch):
    _enable(monkeypatch, test_settings)
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.get_event_trigger_for_dispatch",
        AsyncMock(return_value=(_event_schedule(), _SECRET_HASH)),
    )
    monkeypatch.setattr(
        "teardrop.routers.agent_event_triggers.get_existing_dispatch",
        AsyncMock(return_value=None),
    )
    # Saturate the in-process concurrency counter.
    monkeypatch.setattr("teardrop.routers.agent_event_triggers._inflight_event_runs", 10_000)

    resp = await anon_client.post(
        "/agent/events/tok-abc",
        json={"kind": "payment"},
        headers={"X-Teardrop-Trigger-Secret": _SECRET},
    )
    assert resp.status_code == 429


@pytest.mark.anyio
async def test_dispatch_disabled_feature_returns_404(anon_client, test_settings, monkeypatch):
    test_settings.event_triggers_enabled = False
    monkeypatch.setattr("teardrop.routers.agent_event_triggers.settings", test_settings)
    resp = await anon_client.post(
        "/agent/events/tok-abc",
        json={"kind": "payment"},
        headers={"X-Teardrop-Trigger-Secret": _SECRET},
    )
    assert resp.status_code == 404
