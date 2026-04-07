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


# ── GET /auth/me ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_auth_me_email_user(api_client):
    """Authenticated email user gets their identity claims."""
    resp = await api_client.get("/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "test-user-id"
    assert body["org_id"] == "test-org-id"
    assert body["role"] == "user"
    # No wallet fields for non-SIWE sessions
    assert "address" not in body
    assert "chain_id" not in body


@pytest.mark.anyio
async def test_auth_me_siwe_user(test_settings):
    """SIWE-authenticated user gets wallet fields in the response."""
    from app import app
    from auth import require_auth
    from httpx import ASGITransport, AsyncClient

    async def _mock_siwe_auth():
        return {
            "sub": "siwe-user-id",
            "org_id": "siwe-org-id",
            "role": "user",
            "auth_method": "siwe",
            "address": "0xA03772Fbd16dbf3760B59f1c5921BCeB8A6b2920",
            "chain_id": 1,
            "email": "0xa03772fbd16dbf3760b59f1c5921bceb8a6b2920@wallet",
        }

    app.dependency_overrides[require_auth] = _mock_siwe_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/auth/me")
    finally:
        app.dependency_overrides.pop(require_auth, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "siwe-user-id"
    assert body["auth_method"] == "siwe"
    assert body["address"] == "0xA03772Fbd16dbf3760B59f1c5921BCeB8A6b2920"
    assert body["chain_id"] == 1
    assert body["email"] == "0xa03772fbd16dbf3760b59f1c5921bceb8a6b2920@wallet"


@pytest.mark.anyio
async def test_auth_me_unauthenticated(anon_client):
    """Unauthenticated request returns 401."""
    resp = await anon_client.get("/auth/me")
    assert resp.status_code == 401
