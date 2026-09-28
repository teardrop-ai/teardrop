# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""API tests for the A2A allowlist endpoints.

An allowlist entry exposes a URL to the agent's delegate_to_agent tool, and
``jwt_forward=True`` additionally replays the caller's JWT to that external
agent (a credential-exfiltration vector). Org admins may add any URL; other
members may add only marketplace-registered agents, forced to x402-only
payment, no JWT forwarding, and a bounded cap.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return None


def _seed_pool(*, registry_org_id: str | None = None, delete_row=None, row_exists=None):
    from teardrop.main import app

    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetchrow = AsyncMock(return_value=delete_row)
    conn.fetchval = AsyncMock(return_value=row_exists)
    conn.transaction = MagicMock(return_value=_AsyncContext(conn))

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="INSERT 0 1")
    pool.fetchval = AsyncMock(return_value=registry_org_id)
    pool.acquire = MagicMock(return_value=_AsyncContext(conn))
    app.state.pool = pool
    return pool, conn


@pytest.fixture
def not_promotional(monkeypatch):
    import billing

    monkeypatch.setattr(billing, "is_promotional_credit", AsyncMock(return_value=False))


@pytest.mark.anyio
async def test_unlisted_url_rejected_for_non_admin(api_client, not_promotional):
    _seed_pool(registry_org_id=None)
    resp = await api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com"},
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_jwt_forward_true_rejected_for_non_admin(api_client, not_promotional):
    _seed_pool(registry_org_id="seller-org")
    resp = await api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com", "jwt_forward": True},
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_registry_agent_self_serve_forces_safe_row(api_client, not_promotional, monkeypatch):
    from teardrop.routers.org import a2a as org_a2a

    monkeypatch.setattr(org_a2a.settings, "a2a_delegation_max_cost_usdc", 100_000)
    monkeypatch.setattr(org_a2a.settings, "a2a_delegation_platform_fee_bps", 500)
    pool, conn = _seed_pool(registry_org_id="seller-org")
    resp = await api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://Agent.Example.com/", "require_x402": False},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["source"] == "self_serve"
    assert body["require_x402"] is True
    assert body["jwt_forward"] is False
    assert body["agent_url"] == "https://agent.example.com"
    assert body["max_cost_usdc"] == 95_239
    assert 95_239 + 95_239 * 500 // 10_000 == 100_000
    pool.fetchval.assert_awaited_once()
    insert_args = conn.execute.call_args_list[0].args
    assert insert_args[-4:] == (95_239, True, False, "self_serve")
    assert "a2a_allowed_agent_events" in conn.execute.call_args_list[1].args[0]


@pytest.mark.anyio
async def test_self_serve_cap_above_limit_rejected(api_client, not_promotional, monkeypatch):
    from teardrop.routers.org import a2a as org_a2a

    monkeypatch.setattr(org_a2a.settings, "a2a_delegation_max_cost_usdc", 100_000)
    monkeypatch.setattr(org_a2a.settings, "a2a_delegation_platform_fee_bps", 0)
    _seed_pool(registry_org_id="seller-org")
    resp = await api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com", "max_cost_usdc": 100_001},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_self_serve_rejects_own_agent(api_client, not_promotional):
    _seed_pool(registry_org_id="test-org-id")
    resp = await api_client.post("/a2a/agents", json={"agent_url": "https://agent.example.com"})
    assert resp.status_code == 403
    assert "own" in resp.json()["detail"]


@pytest.mark.anyio
async def test_self_serve_rejects_promotional_credit(api_client, monkeypatch):
    import billing

    monkeypatch.setattr(billing, "is_promotional_credit", AsyncMock(return_value=True))
    _seed_pool(registry_org_id="seller-org")
    resp = await api_client.post("/a2a/agents", json={"agent_url": "https://agent.example.com"})
    assert resp.status_code == 403
    assert "Promotional" in resp.json()["detail"]


@pytest.mark.anyio
async def test_registration_allowed_for_admin(admin_api_client):
    pool, conn = _seed_pool()
    resp = await admin_api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com"},
    )
    assert resp.status_code == 201
    assert resp.json()["jwt_forward"] is False
    assert resp.json()["source"] == "admin"
    pool.fetchval.assert_not_awaited()
    assert conn.execute.await_count == 2


@pytest.mark.anyio
async def test_jwt_forward_true_allowed_for_admin(admin_api_client):
    _seed_pool()
    resp = await admin_api_client.post(
        "/a2a/agents",
        json={"agent_url": "https://agent.example.com", "jwt_forward": True},
    )
    assert resp.status_code == 201
    assert resp.json()["jwt_forward"] is True


@pytest.mark.anyio
async def test_delete_admin_row_rejected_for_non_admin(api_client):
    _seed_pool(delete_row=None, row_exists=1)
    resp = await api_client.delete("/a2a/agents/agent-123")
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_delete_self_serve_row_allowed_for_non_admin(api_client):
    _pool, conn = _seed_pool(
        delete_row={"agent_url": "https://agent.example.com", "source": "self_serve", "max_cost_usdc": 50_000}
    )
    resp = await api_client.delete("/a2a/agents/agent-123")
    assert resp.status_code == 200
    delete_args = conn.fetchrow.await_args.args
    assert delete_args[-1] is False
    assert "a2a_allowed_agent_events" in conn.execute.await_args.args[0]


@pytest.mark.anyio
async def test_add_agent_rate_limited(admin_api_client, monkeypatch):
    """Per-org rate limit guards bulk allowlist injection via a stolen JWT."""
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
