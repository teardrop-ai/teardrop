"""API tests for GET /auth/verify-email and POST /auth/resend-verification."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from users import User


@pytest.fixture(autouse=True)
def _bypass_rate_limit(monkeypatch):
    monkeypatch.setattr("app._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))


def _mock_user(is_verified: bool = False) -> User:
    return User(
        id="user-unverified",
        email="user@example.com",
        org_id="org-1",
        hashed_secret="hashed",
        salt="salt",
        role="user",
        is_active=True,
        is_verified=is_verified,
        created_at=datetime.now(timezone.utc),
    )


# ─── GET /auth/verify-email ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_verify_email_happy_path(anon_client, monkeypatch):
    monkeypatch.setattr("app.consume_verification_token", AsyncMock(return_value="user-123"))
    monkeypatch.setattr("app.mark_user_verified", AsyncMock())

    resp = await anon_client.get("/auth/verify-email?token=valid-token")

    assert resp.status_code == 200
    assert resp.json()["verified"] is True


@pytest.mark.anyio
async def test_verify_email_invalid_token(anon_client, monkeypatch):
    monkeypatch.setattr("app.consume_verification_token", AsyncMock(return_value=None))

    resp = await anon_client.get("/auth/verify-email?token=bad-token")

    assert resp.status_code == 410


@pytest.mark.anyio
async def test_verify_email_calls_mark_verified(anon_client, monkeypatch):
    mark_mock = AsyncMock()
    monkeypatch.setattr("app.consume_verification_token", AsyncMock(return_value="user-abc"))
    monkeypatch.setattr("app.mark_user_verified", mark_mock)

    await anon_client.get("/auth/verify-email?token=tok")

    mark_mock.assert_awaited_once_with("user-abc")


# ─── POST /auth/resend-verification ──────────────────────────────────────────


@pytest.mark.anyio
async def test_resend_verification_unverified_user(anon_client, monkeypatch):
    user = _mock_user(is_verified=False)
    monkeypatch.setattr("app.get_user_by_email", AsyncMock(return_value=user))
    create_vt = AsyncMock(return_value="tok")
    monkeypatch.setattr("app.create_verification_token", create_vt)
    monkeypatch.setattr("app.send_verification_email", AsyncMock())

    resp = await anon_client.post("/auth/resend-verification", json={"email": "user@example.com"})

    assert resp.status_code == 200
    create_vt.assert_awaited_once_with(user.id)


@pytest.mark.anyio
async def test_resend_verification_already_verified_is_noop(anon_client, monkeypatch):
    """Verified users: resend should silently do nothing but still return 200."""
    user = _mock_user(is_verified=True)
    monkeypatch.setattr("app.get_user_by_email", AsyncMock(return_value=user))
    create_vt = AsyncMock(return_value="tok")
    monkeypatch.setattr("app.create_verification_token", create_vt)
    monkeypatch.setattr("app.send_verification_email", AsyncMock())

    resp = await anon_client.post("/auth/resend-verification", json={"email": "user@example.com"})

    assert resp.status_code == 200
    create_vt.assert_not_awaited()


@pytest.mark.anyio
async def test_resend_verification_unknown_email_no_oracle(anon_client, monkeypatch):
    """Unknown email must still return 200 — no disclosure of account existence."""
    monkeypatch.setattr("app.get_user_by_email", AsyncMock(return_value=None))

    resp = await anon_client.post("/auth/resend-verification", json={"email": "ghost@example.com"})

    assert resp.status_code == 200


# ─── /token gate when require_email_verification=True ────────────────────────


@pytest.mark.anyio
async def test_token_gate_blocks_unverified_user(anon_client, test_settings, monkeypatch):
    """When require_email_verification=True, unverified users must receive 403."""
    import config as _config

    monkeypatch.setenv("REQUIRE_EMAIL_VERIFICATION", "true")
    _config.get_settings.cache_clear()
    import app as _app

    monkeypatch.setattr(_app, "settings", _config.get_settings())

    user = _mock_user(is_verified=False)
    monkeypatch.setattr("app.get_user_by_email", AsyncMock(return_value=user))
    monkeypatch.setattr("app.verify_secret", lambda *a, **kw: True)

    resp = await anon_client.post("/token", json={"email": "user@example.com", "secret": "correctpass"})
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_token_gate_passes_verified_user(anon_client, test_settings, monkeypatch):
    """Verified user must still receive a token when the gate is enabled."""
    import config as _config

    monkeypatch.setenv("REQUIRE_EMAIL_VERIFICATION", "true")
    _config.get_settings.cache_clear()
    import app as _app

    monkeypatch.setattr(_app, "settings", _config.get_settings())

    user = _mock_user(is_verified=True)
    monkeypatch.setattr("app.get_user_by_email", AsyncMock(return_value=user))
    monkeypatch.setattr("app.verify_secret", lambda *a, **kw: True)
    monkeypatch.setattr("app.create_refresh_token", AsyncMock(return_value="rt"))

    resp = await anon_client.post("/token", json={"email": "user@example.com", "secret": "correctpass"})
    assert resp.status_code == 200
