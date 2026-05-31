"""API tests for the org-admin gate on the A2A allowlist endpoints.

An allowlist entry exposes an arbitrary URL to the agent's delegate_to_agent
tool, and ``jwt_forward=True`` additionally replays the caller's JWT to that
external agent (a credential-exfiltration vector). Registration and removal are
therefore restricted to org admins; members may only read the allowlist.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _seed_pool():
    from teardrop.main import app

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="INSERT 0 1")
    app.state.pool = pool
    return pool


@pytest.mark.anyio
async def test_registration_rejected_for_non_admin(api_client):
    _seed_pool()
    resp = await api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com"},
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_jwt_forward_true_rejected_for_non_admin(api_client):
    _seed_pool()
    resp = await api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com", "jwt_forward": True},
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_registration_allowed_for_admin(admin_api_client):
    pool = _seed_pool()
    resp = await admin_api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com"},
    )
    assert resp.status_code == 201
    assert resp.json()["jwt_forward"] is False
    pool.execute.assert_awaited_once()


@pytest.mark.anyio
async def test_jwt_forward_true_allowed_for_admin(admin_api_client):
    pool = _seed_pool()
    resp = await admin_api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com", "jwt_forward": True},
    )
    assert resp.status_code == 201
    assert resp.json()["jwt_forward"] is True
    pool.execute.assert_awaited_once()


@pytest.mark.anyio
async def test_delete_rejected_for_non_admin(api_client):
    _seed_pool()
    resp = await api_client.delete("/a2a/agents/agent-123")
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_add_agent_rate_limited(admin_api_client, monkeypatch):
    """Per-org rate limit guards bulk allowlist injection via a stolen admin JWT."""
    _seed_pool()

    async def _denied(_key, _limit):
        return False, 0, 9999999999

    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", _denied)
    resp = await admin_api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com"},
    )
    assert resp.status_code == 429
    assert "Rate limit exceeded" in resp.json()["detail"]
