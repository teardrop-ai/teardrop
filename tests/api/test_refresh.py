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
    monkeypatch.setattr("app.rotate_refresh_token", AsyncMock(return_value=(record, "new-rt")))

    resp = await anon_client.post("/auth/refresh", json={"refresh_token": "old-rt"})

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["refresh_token"] == "new-rt"
    assert body["token_type"] == "bearer"


@pytest.mark.anyio
async def test_refresh_invalid_token_401(anon_client, monkeypatch):
    """Token unknown / truly expired and outside replay window → 401."""
    monkeypatch.setattr("app.rotate_refresh_token", AsyncMock(return_value=None))
    monkeypatch.setattr("app.get_refresh_token_successor", AsyncMock(return_value=None))

    resp = await anon_client.post("/auth/refresh", json={"refresh_token": "bad-token"})

    assert resp.status_code == 401


@pytest.mark.anyio
async def test_refresh_rotation_preserves_claims(anon_client, test_settings, monkeypatch):
    """rotate_refresh_token is called with the correct expire_days from settings."""
    record = _mock_record()
    rotate_mock = AsyncMock(return_value=(record, "new-rt"))
    monkeypatch.setattr("app.rotate_refresh_token", rotate_mock)

    await anon_client.post("/auth/refresh", json={"refresh_token": "old-rt"})

    rotate_mock.assert_awaited_once()
    args, kwargs = rotate_mock.call_args
    assert args[0] == "old-rt"
    assert kwargs["expire_days"] == test_settings.refresh_token_expire_days


@pytest.mark.anyio
async def test_refresh_siwe_token_happy_path(anon_client, test_settings, monkeypatch):
    """Refresh token issued from a SIWE session must work the same way."""
    record = _mock_record(auth_method="siwe")
    monkeypatch.setattr("app.rotate_refresh_token", AsyncMock(return_value=(record, "new-siwe-rt")))

    resp = await anon_client.post("/auth/refresh", json={"refresh_token": "siwe-old-rt"})

    assert resp.status_code == 200
    assert resp.json()["refresh_token"] == "new-siwe-rt"


# ─── Idempotency / atomicity ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_refresh_idempotent_replay_within_window(
    anon_client, test_settings, monkeypatch
):
    """Retry within idempotency window replays the successor token (no lockout).

    Simulates: client sent /auth/refresh, old token was rotated atomically,
    but the HTTP response was lost.  Client retries with the same old token.
    """
    successor = _mock_record()
    # successor.token is what the DB stored as the new token
    successor = RefreshTokenRecord(
        token="successor-rt",
        user_id=successor.user_id,
        org_id=successor.org_id,
        auth_method=successor.auth_method,
        extra_claims=successor.extra_claims,
        # created just now — well within the 60-second window
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    monkeypatch.setattr("app.rotate_refresh_token", AsyncMock(return_value=None))
    monkeypatch.setattr("app.get_refresh_token_successor", AsyncMock(return_value=successor))
    monkeypatch.setenv("REFRESH_TOKEN_IDEMPOTENCY_WINDOW_SECONDS", "60")

    resp = await anon_client.post("/auth/refresh", json={"refresh_token": "old-rt"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["refresh_token"] == "successor-rt"
    assert "access_token" in body


@pytest.mark.anyio
async def test_refresh_replay_window_expired_returns_401(
    anon_client, test_settings, monkeypatch
):
    """Successor exists but was created after the idempotency window → 401.

    Once the replay window closes, old tokens must not grant access.
    """
    monkeypatch.setenv("REFRESH_TOKEN_IDEMPOTENCY_WINDOW_SECONDS", "60")
    # Re-init settings so the env change is picked up.
    import config
    config.get_settings.cache_clear()

    successor = RefreshTokenRecord(
        token="successor-rt",
        user_id="user-123",
        org_id="org-123",
        auth_method="email",
        extra_claims={},
        # created 61 seconds ago — just outside the window
        created_at=datetime.now(timezone.utc) - timedelta(seconds=61),
        expires_at=datetime.now(timezone.utc) + timedelta(days=29),
    )
    monkeypatch.setattr("app.rotate_refresh_token", AsyncMock(return_value=None))
    monkeypatch.setattr("app.get_refresh_token_successor", AsyncMock(return_value=successor))

    resp = await anon_client.post("/auth/refresh", json={"refresh_token": "old-rt"})

    assert resp.status_code == 401
    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_refresh_rotate_called_before_successor_lookup(
    anon_client, test_settings, monkeypatch
):
    """get_refresh_token_successor is only called after rotate_refresh_token returns None.

    If rotate succeeds, we must NOT query the successor table.
    """
    record = _mock_record()
    rotate_mock = AsyncMock(return_value=(record, "new-rt"))
    successor_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.rotate_refresh_token", rotate_mock)
    monkeypatch.setattr("app.get_refresh_token_successor", successor_mock)

    resp = await anon_client.post("/auth/refresh", json={"refresh_token": "old-rt"})

    assert resp.status_code == 200
    successor_mock.assert_not_awaited()


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
