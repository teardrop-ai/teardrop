"""API tests for agent wallet endpoints (POST/GET/DELETE /wallets/agent)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from agent_wallets import AgentWallet

_WALLET = AgentWallet(
    id="aw-1234",
    org_id="test-org-id",
    address="0x71C7656EC7ab88b098defB751B7401B5f6d8976F",
    cdp_account_name="td-test-org-id",
    chain_id=84532,
    wallet_type="eoa",
    is_active=True,
    created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
)


# ─── Feature flag gating ─────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_provision_returns_501_when_disabled(api_client, monkeypatch):
    import config

    monkeypatch.setenv("AGENT_WALLET_ENABLED", "false")
    config.get_settings.cache_clear()

    resp = await api_client.post("/wallets/agent")
    assert resp.status_code == 501
    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_get_returns_501_when_disabled(api_client, monkeypatch):
    import config

    monkeypatch.setenv("AGENT_WALLET_ENABLED", "false")
    config.get_settings.cache_clear()

    resp = await api_client.get("/wallets/agent")
    assert resp.status_code == 501
    config.get_settings.cache_clear()


# ─── POST /wallets/agent ─────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_provision_agent_wallet(api_client, monkeypatch):
    import config

    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("CDP_API_KEY_ID", "k")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "s")
    monkeypatch.setenv("CDP_WALLET_SECRET", "w")
    config.get_settings.cache_clear()

    monkeypatch.setattr("app.create_agent_wallet", AsyncMock(return_value=_WALLET))

    resp = await api_client.post("/wallets/agent")
    assert resp.status_code == 201
    data = resp.json()
    assert data["address"] == _WALLET.address
    assert data["wallet_type"] == "eoa"
    config.get_settings.cache_clear()


# ─── GET /wallets/agent ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_get_agent_wallet(api_client, monkeypatch):
    import config

    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("CDP_API_KEY_ID", "k")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "s")
    monkeypatch.setenv("CDP_WALLET_SECRET", "w")
    config.get_settings.cache_clear()

    monkeypatch.setattr("app.get_agent_wallet", AsyncMock(return_value=_WALLET))

    resp = await api_client.get("/wallets/agent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == _WALLET.id
    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_get_agent_wallet_not_found(api_client, monkeypatch):
    import config

    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("CDP_API_KEY_ID", "k")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "s")
    monkeypatch.setenv("CDP_WALLET_SECRET", "w")
    config.get_settings.cache_clear()

    monkeypatch.setattr("app.get_agent_wallet", AsyncMock(return_value=None))

    resp = await api_client.get("/wallets/agent")
    assert resp.status_code == 404
    config.get_settings.cache_clear()


# ─── DELETE /wallets/agent ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_deactivate_agent_wallet_admin(admin_api_client, monkeypatch):
    import config

    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("CDP_API_KEY_ID", "k")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "s")
    monkeypatch.setenv("CDP_WALLET_SECRET", "w")
    config.get_settings.cache_clear()

    monkeypatch.setattr("app.deactivate_agent_wallet", AsyncMock(return_value=True))

    resp = await admin_api_client.delete("/wallets/agent")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deactivated"
    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_deactivate_requires_admin(api_client, monkeypatch):
    import config

    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("CDP_API_KEY_ID", "k")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "s")
    monkeypatch.setenv("CDP_WALLET_SECRET", "w")
    config.get_settings.cache_clear()

    resp = await api_client.delete("/wallets/agent")
    # Non-admin user should get 403.
    assert resp.status_code == 403
    config.get_settings.cache_clear()


# ─── Auth gating ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_provision_requires_auth(anon_client):
    resp = await anon_client.post("/wallets/agent")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_get_requires_auth(anon_client):
    resp = await anon_client.get("/wallets/agent")
    assert resp.status_code == 401
