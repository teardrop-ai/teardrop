"""API tests for the decision-graph surface: GET /agent/decisions and
PATCH /agent/runs/{run_id}/outcome.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

_NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)

_DECISION_ROW = {
    "id": "dec-1",
    "run_id": "run-123",
    "task_class": "liquidation_risk",
    "action": "flag_liquidation_risk",
    "reasoning": "health factor below 1.05",
    "confidence": 0.85,
    "tool_names": ["get_liquidation_risk"],
    "outcome": 0,
    "outcome_source": "",
    "created_at": _NOW,
}


# ─── GET /agent/decisions ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_list_agent_decisions(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.agent.list_run_decisions", AsyncMock(return_value=[_DECISION_ROW]))

    resp = await api_client.get("/agent/decisions")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["run_id"] == "run-123"
    assert body["items"][0]["action"] == "flag_liquidation_risk"
    assert body["next_cursor"] == _NOW.isoformat()


@pytest.mark.anyio
async def test_list_agent_decisions_empty(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.agent.list_run_decisions", AsyncMock(return_value=[]))

    resp = await api_client.get("/agent/decisions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


@pytest.mark.anyio
async def test_list_agent_decisions_requires_auth(anon_client):
    resp = await anon_client.get("/agent/decisions")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_list_agent_decisions_rejects_bad_cursor(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.agent.list_run_decisions", AsyncMock(return_value=[]))

    resp = await api_client.get("/agent/decisions", params={"cursor": "not-a-date"})
    assert resp.status_code == 400


# ─── PATCH /agent/runs/{run_id}/outcome ────────────────────────────────────────


@pytest.mark.anyio
async def test_set_agent_run_outcome(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.agent.get_invoice_by_run", AsyncMock(return_value={"run_id": "run-123"}))
    monkeypatch.setattr("teardrop.routers.agent.backfill_decision_outcome", AsyncMock(return_value=True))

    resp = await api_client.patch("/agent/runs/run-123/outcome", json={"rating": 1})
    assert resp.status_code == 200
    assert resp.json()["status"] == "recorded"


@pytest.mark.anyio
async def test_set_agent_run_outcome_unknown_run(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.agent.get_invoice_by_run", AsyncMock(return_value=None))

    resp = await api_client.patch("/agent/runs/run-999/outcome", json={"rating": 1})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_set_agent_run_outcome_no_decision_record(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.agent.get_invoice_by_run", AsyncMock(return_value={"run_id": "run-123"}))
    monkeypatch.setattr("teardrop.routers.agent.backfill_decision_outcome", AsyncMock(return_value=False))

    resp = await api_client.patch("/agent/runs/run-123/outcome", json={"rating": 1})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_set_agent_run_outcome_rejects_invalid_rating(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.agent.get_invoice_by_run", AsyncMock(return_value={"run_id": "run-123"}))

    resp = await api_client.patch("/agent/runs/run-123/outcome", json={"rating": 5})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_set_agent_run_outcome_requires_auth(anon_client):
    resp = await anon_client.patch("/agent/runs/run-123/outcome", json={"rating": 1})
    assert resp.status_code == 401
