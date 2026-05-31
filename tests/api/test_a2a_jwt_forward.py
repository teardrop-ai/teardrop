"""API tests for the jwt_forward admin gate on POST /a2a/agents.

``jwt_forward=True`` causes the caller's JWT to be replayed to an external agent
(a credential-exfiltration vector), so only org admins may register such an
allowlist entry. Non-admins may still register agents without jwt_forward.
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
async def test_jwt_forward_true_rejected_for_non_admin(api_client):
    _seed_pool()
    resp = await api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com", "jwt_forward": True},
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower()


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
async def test_no_jwt_forward_allowed_for_non_admin(api_client):
    pool = _seed_pool()
    resp = await api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com", "jwt_forward": False},
    )
    assert resp.status_code == 201
    assert resp.json()["jwt_forward"] is False
    pool.execute.assert_awaited_once()


@pytest.mark.anyio
async def test_jwt_forward_defaults_false_for_non_admin(api_client):
    _seed_pool()
    resp = await api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com"},
    )
    assert resp.status_code == 201
    assert resp.json()["jwt_forward"] is False


@pytest.mark.anyio
async def test_add_agent_rate_limited(api_client, monkeypatch):
    """Per-org rate limit guards bulk allowlist injection via a stolen JWT."""
    _seed_pool()

    async def _denied(_key, _limit):
        return False, 0, 9999999999

    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", _denied)
    resp = await api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com"},
    )
    assert resp.status_code == 429
    assert "Rate limit exceeded" in resp.json()["detail"]
