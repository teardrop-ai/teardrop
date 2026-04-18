"""API tests for wallet endpoints (POST /wallets/link, GET /wallets/me, DELETE)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

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


# ─── POST /wallets/link ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_link_wallet_requires_auth(anon_client):
    resp = await anon_client.post(
        "/wallets/link",
        json={"siwe_message": "msg", "siwe_signature": "0xsig"},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_link_wallet_malformed_siwe_message_returns_400(api_client, monkeypatch):
    """A completely unparseable SIWE message should return 400."""

    class FailSiwe:
        @staticmethod
        def from_message(m):
            raise ValueError("bad message")

    monkeypatch.setattr("siwe.SiweMessage", FailSiwe)

    resp = await api_client.post(
        "/wallets/link",
        json={"siwe_message": "not a real siwe message", "siwe_signature": "0xsig"},
    )
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_link_wallet_expired_nonce_returns_401(api_client, monkeypatch):
    """Using an expired or already-consumed nonce should return 401."""
    mock_msg = MagicMock()
    mock_msg.domain = "0.0.0.0"
    mock_msg.nonce = "oldnonce123"
    mock_msg.address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    mock_msg.chain_id = "1"
    mock_msg.verify = MagicMock()  # signature valid, but nonce expired

    class FakeSiwe:
        @staticmethod
        def from_message(m):
            return mock_msg

    monkeypatch.setattr("siwe.SiweMessage", FakeSiwe)
    monkeypatch.setattr("app.consume_nonce", AsyncMock(return_value=False))

    resp = await api_client.post(
        "/wallets/link",
        json={"siwe_message": "any", "siwe_signature": "0xsig"},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_link_wallet_invalid_signature_returns_401(api_client, monkeypatch):
    """An invalid SIWE signature should return 401 WITHOUT consuming nonce."""
    import siwe as siwe_errors

    consume_mock = AsyncMock(return_value=True)

    mock_msg = MagicMock()
    mock_msg.domain = "0.0.0.0"
    mock_msg.nonce = "validnonce"
    mock_msg.verify = MagicMock(side_effect=siwe_errors.InvalidSignature)

    class FakeSiwe:
        @staticmethod
        def from_message(m):
            return mock_msg

    monkeypatch.setattr("siwe.SiweMessage", FakeSiwe)
    monkeypatch.setattr("app.consume_nonce", consume_mock)

    resp = await api_client.post(
        "/wallets/link",
        json={"siwe_message": "any", "siwe_signature": "0xbadsig"},
    )
    assert resp.status_code == 401
    # Nonce must NOT be consumed when signature is invalid
    consume_mock.assert_not_called()


@pytest.mark.anyio
async def test_link_wallet_already_linked_returns_409(api_client, monkeypatch):
    """Linking a wallet address that is already linked should return 409."""
    mock_msg = MagicMock()
    mock_msg.domain = "0.0.0.0"
    mock_msg.nonce = "validnonce"
    mock_msg.verify = MagicMock()
    mock_msg.address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    mock_msg.chain_id = "1"

    class FakeSiwe:
        @staticmethod
        def from_message(m):
            return mock_msg

    monkeypatch.setattr("siwe.SiweMessage", FakeSiwe)
    monkeypatch.setattr("app.consume_nonce", AsyncMock(return_value=True))
    monkeypatch.setattr("app.get_wallet_by_address", AsyncMock(return_value=_WALLET))

    resp = await api_client.post(
        "/wallets/link",
        json={"siwe_message": "any", "siwe_signature": "0xvalid"},
    )
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_link_wallet_success_returns_201(api_client, monkeypatch):
    """Valid SIWE + unused nonce + new address → 201 with wallet details."""
    mock_msg = MagicMock()
    mock_msg.domain = "0.0.0.0"
    mock_msg.nonce = "freshonce"
    mock_msg.verify = MagicMock()
    mock_msg.address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    mock_msg.chain_id = "1"

    new_wallet = Wallet(
        id="wallet-new",
        address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        chain_id=1,
        user_id="test-user-id",
        org_id="test-org-id",
        is_primary=False,
        created_at=datetime.now(timezone.utc),
    )

    class FakeSiwe:
        @staticmethod
        def from_message(m):
            return mock_msg

    monkeypatch.setattr("siwe.SiweMessage", FakeSiwe)
    monkeypatch.setattr("app.consume_nonce", AsyncMock(return_value=True))
    monkeypatch.setattr("app.get_wallet_by_address", AsyncMock(return_value=None))
    monkeypatch.setattr("app.create_wallet", AsyncMock(return_value=new_wallet))

    resp = await api_client.post(
        "/wallets/link",
        json={"siwe_message": "any", "siwe_signature": "0xvalid"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "wallet-new"
    assert body["address"] == new_wallet.address
