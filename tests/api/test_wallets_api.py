"""API tests for wallet endpoints (POST /wallets/link, GET /wallets/me, DELETE)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from wallets import Wallet

_WALLET = Wallet(
    id="wallet-abc",
    address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    chain_id=1,
    user_id="test-user-id",
    org_id="test-org-id",
    is_primary=False,
    created_at=datetime.now(timezone.utc),
)


@pytest.mark.anyio
async def test_list_wallets_empty(api_client, monkeypatch):
    monkeypatch.setattr("app.get_wallets_by_user", AsyncMock(return_value=[]))

    resp = await api_client.get("/wallets/me")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_list_wallets_returns_wallets(api_client, monkeypatch):
    monkeypatch.setattr("app.get_wallets_by_user", AsyncMock(return_value=[_WALLET]))

    resp = await api_client.get("/wallets/me")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["address"] == _WALLET.address


@pytest.mark.anyio
async def test_list_wallets_requires_auth(anon_client):
    resp = await anon_client.get("/wallets/me")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_delete_wallet_success(api_client, monkeypatch):
    monkeypatch.setattr("app.delete_wallet", AsyncMock(return_value=True))

    resp = await api_client.delete(f"/wallets/{_WALLET.id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


@pytest.mark.anyio
async def test_delete_wallet_not_found(api_client, monkeypatch):
    monkeypatch.setattr("app.delete_wallet", AsyncMock(return_value=False))

    resp = await api_client.delete("/wallets/nonexistent-id")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_delete_wallet_requires_auth(anon_client):
    resp = await anon_client.delete("/wallets/some-id")
    assert resp.status_code == 401
