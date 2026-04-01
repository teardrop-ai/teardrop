"""API tests for GET /auth/siwe/nonce and POST /token SIWE flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.anyio
async def test_get_siwe_nonce(anon_client, monkeypatch):
    monkeypatch.setattr("app.create_nonce", AsyncMock(return_value="test-nonce-abc"))

    resp = await anon_client.get("/auth/siwe/nonce")
    assert resp.status_code == 200
    body = resp.json()
    assert body["nonce"] == "test-nonce-abc"


@pytest.mark.anyio
async def test_siwe_login_happy_path(anon_client, monkeypatch, test_settings):
    """SIWE login with a valid signature should return a JWT."""
    from datetime import datetime, timezone
    from wallets import Wallet

    mock_wallet = Wallet(
        id="wallet-1",
        address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        chain_id=1,
        user_id="user-siwe",
        org_id="org-1",
        is_primary=True,
        created_at=datetime.now(timezone.utc),
    )

    mock_msg = MagicMock()
    mock_msg.address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    mock_msg.chain_id = "1"
    mock_msg.domain = test_settings.effective_siwe_domain
    mock_msg.verify = MagicMock()  # no exception = valid signature

    mock_siwe_cls = MagicMock()
    mock_siwe_cls.from_message = MagicMock(return_value=mock_msg)

    monkeypatch.setattr("app.consume_nonce", AsyncMock(return_value=True))
    monkeypatch.setattr("app.get_wallet_by_address", AsyncMock(return_value=mock_wallet))

    with patch("siwe.SiweMessage", mock_siwe_cls):
        resp = await anon_client.post(
            "/token",
            json={
                "siwe_message": "some-siwe-message",
                "siwe_signature": "0xsignature",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body


@pytest.mark.anyio
async def test_siwe_login_expired_nonce(anon_client, monkeypatch, test_settings):
    mock_msg = MagicMock()
    mock_msg.address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    mock_msg.chain_id = "1"
    mock_msg.domain = test_settings.effective_siwe_domain
    mock_msg.verify = MagicMock()

    mock_siwe_cls = MagicMock()
    mock_siwe_cls.from_message = MagicMock(return_value=mock_msg)

    monkeypatch.setattr("app.consume_nonce", AsyncMock(return_value=False))

    with patch("siwe.SiweMessage", mock_siwe_cls):
        resp = await anon_client.post(
            "/token",
            json={
                "siwe_message": "some-siwe-message",
                "siwe_signature": "0xsig",
            },
        )

    assert resp.status_code == 401


@pytest.mark.anyio
async def test_siwe_login_bad_signature(anon_client, monkeypatch, test_settings):
    mock_msg = MagicMock()
    mock_msg.address = "0xaddr"
    mock_msg.chain_id = "1"
    mock_msg.domain = test_settings.effective_siwe_domain
    mock_msg.verify = MagicMock(side_effect=Exception("Invalid signature"))

    mock_siwe_cls = MagicMock()
    mock_siwe_cls.from_message = MagicMock(return_value=mock_msg)

    monkeypatch.setattr("app.consume_nonce", AsyncMock(return_value=True))

    with patch("siwe.SiweMessage", mock_siwe_cls):
        resp = await anon_client.post(
            "/token",
            json={
                "siwe_message": "some-siwe-message",
                "siwe_signature": "0xbadsig",
            },
        )

    assert resp.status_code in (400, 401)
