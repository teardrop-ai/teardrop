"""API tests for POST /auth/refresh and POST /auth/logout."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from users import RefreshTokenRecord


@pytest.fixture(autouse=True)
def _bypass_rate_limit(monkeypatch):
    monkeypatch.setattr("app._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))


def _mock_record(auth_method: str = "email") -> RefreshTokenRecord:
    now = datetime.now(timezone.utc)
    return RefreshTokenRecord(
        token="old-rt",
        user_id="user-123",
        org_id="org-123",
        auth_method=auth_method,
        extra_claims={
            "org_id": "org-123",
            "email": "alice@example.com",
            "role": "user",
            "auth_method": auth_method,
        },
        created_at=now,
        expires_at=now + timedelta(days=30),
    )


# ─── POST /auth/refresh ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_refresh_happy_path(anon_client, test_settings, monkeypatch):
    """Valid refresh token issues new access token + new (rotated) refresh token."""
    record = _mock_record()
    monkeypatch.setattr("app.consume_refresh_token", AsyncMock(return_value=record))
    monkeypatch.setattr("app.create_refresh_token", AsyncMock(return_value="new-rt"))

    resp = await anon_client.post("/auth/refresh", json={"refresh_token": "old-rt"})

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["refresh_token"] == "new-rt"
    assert body["token_type"] == "bearer"


@pytest.mark.anyio
async def test_refresh_invalid_token_401(anon_client, monkeypatch):
    monkeypatch.setattr("app.consume_refresh_token", AsyncMock(return_value=None))

    resp = await anon_client.post("/auth/refresh", json={"refresh_token": "bad-token"})

    assert resp.status_code == 401


@pytest.mark.anyio
async def test_refresh_issues_new_rt_with_same_claims(anon_client, test_settings, monkeypatch):
    """New refresh token must be created with the same claims as the old one."""
    record = _mock_record()
    monkeypatch.setattr("app.consume_refresh_token", AsyncMock(return_value=record))
    create_rt_mock = AsyncMock(return_value="new-rt")
    monkeypatch.setattr("app.create_refresh_token", create_rt_mock)

    await anon_client.post("/auth/refresh", json={"refresh_token": "old-rt"})

    create_rt_mock.assert_awaited_once()
    _, kwargs = create_rt_mock.call_args
    assert kwargs["user_id"] == record.user_id
    assert kwargs["org_id"] == record.org_id
    assert kwargs["auth_method"] == record.auth_method
    assert kwargs["extra_claims"] == record.extra_claims


@pytest.mark.anyio
async def test_refresh_siwe_token_happy_path(anon_client, test_settings, monkeypatch):
    """Refresh token issued from a SIWE session must work the same way."""
    record = _mock_record(auth_method="siwe")
    monkeypatch.setattr("app.consume_refresh_token", AsyncMock(return_value=record))
    monkeypatch.setattr("app.create_refresh_token", AsyncMock(return_value="new-siwe-rt"))

    resp = await anon_client.post("/auth/refresh", json={"refresh_token": "siwe-old-rt"})

    assert resp.status_code == 200
    assert resp.json()["refresh_token"] == "new-siwe-rt"


# ─── POST /auth/logout ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_logout_revokes_token(api_client, monkeypatch):
    revoke_mock = AsyncMock()
    monkeypatch.setattr("app.revoke_refresh_token", revoke_mock)

    resp = await api_client.post("/auth/logout", json={"refresh_token": "some-rt"})

    assert resp.status_code == 204
    revoke_mock.assert_awaited_once_with("some-rt")


@pytest.mark.anyio
async def test_logout_no_auth_401(anon_client):
    resp = await anon_client.post("/auth/logout", json={"refresh_token": "some-rt"})
    assert resp.status_code == 401


# ─── /token includes refresh_token ───────────────────────────────────────────


@pytest.mark.anyio
async def test_token_email_flow_returns_refresh_token(anon_client, test_settings, monkeypatch):
    """/token email flow must now include refresh_token in the response."""
    from datetime import datetime, timezone

    from users import User

    mock_user = User(
        id="user-123",
        email="alice@example.com",
        org_id="org-123",
        hashed_secret="ignored",
        salt="ignored",
        role="user",
        is_active=True,
        is_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr("app.get_user_by_email", AsyncMock(return_value=mock_user))
    monkeypatch.setattr("app.verify_secret", lambda *a, **kw: True)
    monkeypatch.setattr("app.create_refresh_token", AsyncMock(return_value="rt-from-token"))

    resp = await anon_client.post(
        "/token", json={"email": "alice@example.com", "secret": "correctpass"}
    )

    assert resp.status_code == 200
    assert resp.json()["refresh_token"] == "rt-from-token"
