"""API tests for A2A delegation admin endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _mock_pool(*, execute_return="INSERT 0 1", fetch_return=None, execute_side_effect=None):
    """Create a mock asyncpg pool with configurable responses."""
    pool = MagicMock()
    pool.execute = AsyncMock(return_value=execute_return, side_effect=execute_side_effect)
    pool.fetch = AsyncMock(return_value=fetch_return or [])
    return pool


@pytest.mark.anyio
async def test_add_a2a_agent(admin_api_client, monkeypatch):
    from app import app

    pool = _mock_pool()
    app.state.pool = pool

    resp = await admin_api_client.post(
        "/admin/a2a/agents",
        json={"org_id": "org-1", "agent_url": "https://agent.example.com", "label": "Test Agent"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["org_id"] == "org-1"
    assert body["agent_url"] == "https://agent.example.com"
    assert body["label"] == "Test Agent"
    assert "id" in body
    pool.execute.assert_awaited_once()


@pytest.mark.anyio
async def test_add_a2a_agent_duplicate(admin_api_client):
    import asyncpg

    from app import app

    pool = _mock_pool(execute_side_effect=asyncpg.UniqueViolationError("duplicate"))
    app.state.pool = pool

    resp = await admin_api_client.post(
        "/admin/a2a/agents",
        json={"org_id": "org-1", "agent_url": "https://agent.example.com"},
    )
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_add_a2a_agent_requires_admin(api_client):
    resp = await api_client.post(
        "/admin/a2a/agents",
        json={"org_id": "org-1", "agent_url": "https://agent.example.com"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_list_a2a_agents(admin_api_client):
    from datetime import datetime, timezone

    from app import app

    mock_rows = [
        {
            "id": "agent-1",
            "org_id": "org-1",
            "agent_url": "https://agent1.example.com",
            "label": "Agent One",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
    ]
    pool = _mock_pool(fetch_return=mock_rows)
    app.state.pool = pool

    resp = await admin_api_client.get("/admin/a2a/agents/org-1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["agent_url"] == "https://agent1.example.com"


@pytest.mark.anyio
async def test_list_a2a_agents_empty(admin_api_client):
    from app import app

    pool = _mock_pool(fetch_return=[])
    app.state.pool = pool

    resp = await admin_api_client.get("/admin/a2a/agents/org-1")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_delete_a2a_agent(admin_api_client):
    from app import app

    pool = _mock_pool(execute_return="DELETE 1")
    app.state.pool = pool

    resp = await admin_api_client.delete("/admin/a2a/agents/agent-123")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == "agent-123"


@pytest.mark.anyio
async def test_delete_a2a_agent_not_found(admin_api_client):
    from app import app

    pool = _mock_pool(execute_return="DELETE 0")
    app.state.pool = pool

    resp = await admin_api_client.delete("/admin/a2a/agents/nonexistent")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_delete_a2a_agent_requires_admin(api_client):
    resp = await api_client.delete("/admin/a2a/agents/agent-123")
    assert resp.status_code == 403
