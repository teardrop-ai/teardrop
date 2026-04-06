"""API tests for POST /token — all three authentication flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import jwt
import pytest


@pytest.mark.anyio
async def test_token_email_flow_success(anon_client, monkeypatch):
    from users import User
    from datetime import datetime, timezone

    mock_user = User(
        id="user-123",
        email="alice@example.com",
        org_id="org-123",
        hashed_secret="ignored",
        salt="ignored",
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )

    monkeypatch.setattr("app.get_user_by_email", AsyncMock(return_value=mock_user))
    monkeypatch.setattr("app.verify_secret", lambda *a, **kw: True)

    resp = await anon_client.post(
        "/token",
        json={"email": "alice@example.com", "secret": "s3cr3t!"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"

    # Verify auth_method claim is present in the JWT
    from config import get_settings
    settings = get_settings()
    claims = jwt.decode(
        body["access_token"],
        settings.jwt_public_key,
        algorithms=[settings.jwt_algorithm],
        issuer=settings.jwt_issuer,
    )
    assert claims["auth_method"] == "email"
    assert claims["org_id"] == "org-123"


@pytest.mark.anyio
async def test_token_email_flow_wrong_credentials(anon_client, monkeypatch):
    monkeypatch.setattr("app.get_user_by_email", AsyncMock(return_value=None))

    resp = await anon_client.post(
        "/token",
        json={"email": "nobody@example.com", "secret": "wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_token_client_credentials_success(anon_client, test_settings, monkeypatch):
    # DB lookup returns None → falls back to config credential
    monkeypatch.setattr("app.get_client_credential_by_id", AsyncMock(return_value=None))
    resp = await anon_client.post(
        "/token",
        json={
            "client_id": test_settings.jwt_client_id,
            "client_secret": test_settings.jwt_client_secret,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body

    # Verify auth_method claim is present in the JWT
    claims = jwt.decode(
        body["access_token"],
        test_settings.jwt_public_key,
        algorithms=[test_settings.jwt_algorithm],
        issuer=test_settings.jwt_issuer,
    )
    assert claims["auth_method"] == "client_credentials"
    assert "org_id" in claims


@pytest.mark.anyio
async def test_token_client_credentials_wrong_secret(anon_client, test_settings, monkeypatch):
    # DB lookup returns None → falls back to config credential
    monkeypatch.setattr("app.get_client_credential_by_id", AsyncMock(return_value=None))
    resp = await anon_client.post(
        "/token",
        json={
            "client_id": test_settings.jwt_client_id,
            "client_secret": "totally-wrong",
        },
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_token_missing_all_fields_returns_400(anon_client):
    resp = await anon_client.post("/token", json={})
    assert resp.status_code in (400, 422)


@pytest.mark.anyio
async def test_token_no_auth_header_returns_401(anon_client):
    """A protected endpoint without a token should return 401."""
    resp = await anon_client.get("/usage/me")
    assert resp.status_code == 401


# ─── DB-backed client credentials ────────────────────────────────────────────


@pytest.mark.anyio
async def test_token_db_client_credentials_success(anon_client, test_settings, monkeypatch):
    """DB-backed credential returns org_id from the DB row (not empty string)."""
    from datetime import datetime, timezone
    from users import OrgClientCredential

    mock_cred = OrgClientCredential(
        client_id="db-client-id",
        org_id="org-abc",
        hashed_secret="ignored",
        salt="ignored",
        created_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr("app.get_client_credential_by_id", AsyncMock(return_value=mock_cred))
    monkeypatch.setattr("app.verify_secret", lambda secret, hashed, salt: True)

    resp = await anon_client.post(
        "/token",
        json={"client_id": "db-client-id", "client_secret": "some-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body

    claims = jwt.decode(
        body["access_token"],
        test_settings.jwt_public_key,
        algorithms=[test_settings.jwt_algorithm],
        issuer=test_settings.jwt_issuer,
    )
    assert claims["auth_method"] == "client_credentials"
    assert claims["org_id"] == "org-abc"


@pytest.mark.anyio
async def test_token_db_client_credentials_wrong_secret(anon_client, monkeypatch):
    """DB-backed credential with wrong secret returns 401."""
    from datetime import datetime, timezone
    from users import OrgClientCredential

    mock_cred = OrgClientCredential(
        client_id="db-client-id",
        org_id="org-abc",
        hashed_secret="ignored",
        salt="ignored",
        created_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr("app.get_client_credential_by_id", AsyncMock(return_value=mock_cred))
    monkeypatch.setattr("app.verify_secret", lambda secret, hashed, salt: False)

    resp = await anon_client.post(
        "/token",
        json={"client_id": "db-client-id", "client_secret": "wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_token_unknown_client_id_falls_back_to_config(anon_client, test_settings, monkeypatch):
    """Unknown client_id (not in DB) falls back to config-based check."""
    monkeypatch.setattr("app.get_client_credential_by_id", AsyncMock(return_value=None))

    resp = await anon_client.post(
        "/token",
        json={
            "client_id": test_settings.jwt_client_id,
            "client_secret": test_settings.jwt_client_secret,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    claims = jwt.decode(
        body["access_token"],
        test_settings.jwt_public_key,
        algorithms=[test_settings.jwt_algorithm],
        issuer=test_settings.jwt_issuer,
    )
    # Config-based fallback has empty org_id
    assert claims["org_id"] == ""


@pytest.mark.anyio
async def test_token_unknown_client_id_wrong_config_secret(anon_client, test_settings, monkeypatch):
    """client_id not in DB and wrong config credential → 401."""
    monkeypatch.setattr("app.get_client_credential_by_id", AsyncMock(return_value=None))

    resp = await anon_client.post(
        "/token",
        json={"client_id": test_settings.jwt_client_id, "client_secret": "wrong"},
    )
    assert resp.status_code == 401
