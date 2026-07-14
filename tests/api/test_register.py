"""API tests for POST /register — self-serve org registration."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from teardrop.users import Org, User


@pytest.fixture(autouse=True)
def _bypass_rate_limit(monkeypatch):
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))


def _mock_org(org_id: str = "org-new") -> Org:
    return Org(id=org_id, name="Alice Inc", created_at=datetime.now(timezone.utc))


def _mock_user(org_id: str = "org-new") -> User:
    return User(
        id="user-new",
        email="alice@example.com",
        org_id=org_id,
        hashed_secret="hashed",
        salt="salt",
        role="user",
        is_active=True,
        is_verified=False,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.anyio
async def test_register_happy_path(anon_client, monkeypatch):
    org, user = _mock_org(), _mock_user()
    monkeypatch.setattr("teardrop.routers.auth.register_org_and_user", AsyncMock(return_value=(org, user)))
    monkeypatch.setattr("teardrop.routers.auth.create_verification_token", AsyncMock(return_value="tok123"))
    monkeypatch.setattr("teardrop.routers.auth.send_verification_email", AsyncMock())
    monkeypatch.setattr("teardrop.routers.auth.create_refresh_token", AsyncMock(return_value="rt-abc"))

    resp = await anon_client.post(
        "/register",
        json={"org_name": "Alice Inc", "email": "alice@example.com", "password": "strongpass1"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert body["refresh_token"] == "rt-abc"


@pytest.mark.anyio
async def test_register_response_unchanged_when_onboarding_credit_enabled(anon_client, monkeypatch):
    """Onboarding credit must not leak into the /register response."""
    org, user = _mock_org(), _mock_user()
    monkeypatch.setattr("teardrop.routers.auth.settings.onboarding_credit_enabled", True)
    monkeypatch.setattr("teardrop.routers.auth.settings.onboarding_credit_usdc", 500_000)
    monkeypatch.setattr("teardrop.routers.auth.register_org_and_user", AsyncMock(return_value=(org, user)))
    monkeypatch.setattr("teardrop.routers.auth.create_verification_token", AsyncMock(return_value="tok123"))
    monkeypatch.setattr("teardrop.routers.auth.send_verification_email", AsyncMock())
    monkeypatch.setattr("teardrop.routers.auth.create_refresh_token", AsyncMock(return_value="rt-abc"))

    resp = await anon_client.post(
        "/register",
        json={"org_name": "Alice Inc", "email": "alice@example.com", "password": "strongpass1"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert set(body.keys()) == {"access_token", "token_type", "refresh_token", "expires_in"}
    assert "credit" not in body
    assert "onboarding" not in body


@pytest.mark.anyio
async def test_register_issues_verification_token(anon_client, monkeypatch):
    """register() must call create_verification_token to fire the verification flow."""
    org, user = _mock_org(), _mock_user()
    monkeypatch.setattr("teardrop.routers.auth.register_org_and_user", AsyncMock(return_value=(org, user)))
    create_vt = AsyncMock(return_value="tok-xyz")
    monkeypatch.setattr("teardrop.routers.auth.create_verification_token", create_vt)
    monkeypatch.setattr("teardrop.routers.auth.send_verification_email", AsyncMock())
    monkeypatch.setattr("teardrop.routers.auth.create_refresh_token", AsyncMock(return_value="rt"))

    await anon_client.post(
        "/register",
        json={"org_name": "Org", "email": "alice@example.com", "password": "strongpass1"},
    )

    create_vt.assert_awaited_once_with(user.id)


@pytest.mark.anyio
async def test_register_duplicate_raises_409(anon_client, monkeypatch):
    import asyncpg

    monkeypatch.setattr(
        "teardrop.routers.auth.register_org_and_user",
        AsyncMock(side_effect=asyncpg.UniqueViolationError()),
    )

    resp = await anon_client.post(
        "/register",
        json={"org_name": "Dup Org", "email": "dup@example.com", "password": "strongpass1"},
    )

    assert resp.status_code == 409


@pytest.mark.anyio
async def test_register_weak_password_422(anon_client):
    resp = await anon_client.post(
        "/register",
        json={"org_name": "Org", "email": "a@b.com", "password": "short"},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_register_missing_email_422(anon_client):
    resp = await anon_client.post("/register", json={"org_name": "Org", "password": "strongpass1"})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_register_missing_org_name_422(anon_client):
    resp = await anon_client.post("/register", json={"email": "a@b.com", "password": "strongpass1"})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_register_no_auth_required(anon_client, monkeypatch):
    """Public endpoint — must not return 401 even without a Bearer token."""
    org, user = _mock_org(), _mock_user()
    monkeypatch.setattr("teardrop.routers.auth.register_org_and_user", AsyncMock(return_value=(org, user)))
    monkeypatch.setattr("teardrop.routers.auth.create_verification_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr("teardrop.routers.auth.send_verification_email", AsyncMock())
    monkeypatch.setattr("teardrop.routers.auth.create_refresh_token", AsyncMock(return_value="rt"))

    resp = await anon_client.post(
        "/register",
        json={"org_name": "Public Org", "email": "pub@example.com", "password": "strongpass1"},
    )
    assert resp.status_code != 401


# ─── Input validation ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_register_invalid_email_format_422(anon_client):
    resp = await anon_client.post(
        "/register",
        json={"org_name": "Org", "email": "notanemail", "password": "strongpass1"},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_register_email_normalized_to_lowercase(anon_client, monkeypatch):
    """Email submitted in mixed case must be stored lowercase."""
    org, user = _mock_org(), _mock_user()
    captured: dict = {}

    async def fake_register(org_name: str, email: str, secret: str):
        captured["email"] = email
        return org, user

    monkeypatch.setattr("teardrop.routers.auth.register_org_and_user", fake_register)
    monkeypatch.setattr("teardrop.routers.auth.create_verification_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr("teardrop.routers.auth.send_verification_email", AsyncMock())
    monkeypatch.setattr("teardrop.routers.auth.create_refresh_token", AsyncMock(return_value="rt"))

    resp = await anon_client.post(
        "/register",
        json={"org_name": "Alice Inc", "email": "ALICE@EXAMPLE.COM", "password": "strongpass1"},
    )

    assert resp.status_code == 201
    assert captured["email"] == "alice@example.com"


@pytest.mark.anyio
async def test_register_password_missing_digit_422(anon_client):
    resp = await anon_client.post(
        "/register",
        json={"org_name": "Org", "email": "alice@example.com", "password": "nodigitshere"},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_register_per_email_rate_limit_429(anon_client, monkeypatch):
    """Per-email bucket blocks repeated registrations for the same address even across IPs."""

    async def _rate_limit(key: str, limit: int):
        if key.startswith("register:email:"):
            return (False, 0, 0)
        return (True, 59, 0)

    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", _rate_limit)

    resp = await anon_client.post(
        "/register",
        json={"org_name": "Org", "email": "alice@example.com", "password": "strongpass1"},
    )
    assert resp.status_code == 429


@pytest.mark.anyio
async def test_register_disabled_when_public_registration_off(anon_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.auth.settings.allow_public_registration", False)
    create_org_user = AsyncMock()
    monkeypatch.setattr("teardrop.routers.auth.register_org_and_user", create_org_user)

    resp = await anon_client.post(
        "/register",
        json={"org_name": "Org", "email": "alice@example.com", "password": "strongpass1"},
    )

    assert resp.status_code == 403
    create_org_user.assert_not_awaited()


@pytest.mark.anyio
async def test_register_turnstile_enabled_rejects_invalid_token(anon_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.auth.settings.allow_public_registration", True)
    monkeypatch.setattr("teardrop.routers.auth.settings.turnstile_secret_key", "ts_secret")
    verify_turnstile = AsyncMock(return_value=False)
    monkeypatch.setattr("teardrop.routers.auth.verify_turnstile", verify_turnstile)

    resp = await anon_client.post(
        "/register",
        json={
            "org_name": "Org",
            "email": "alice@example.com",
            "password": "strongpass1",
            "captcha_token": "bad-token",
        },
    )

    assert resp.status_code == 403
    verify_turnstile.assert_awaited_once()


@pytest.mark.anyio
async def test_register_turnstile_enabled_accepts_valid_token(anon_client, monkeypatch):
    org, user = _mock_org(), _mock_user()
    monkeypatch.setattr("teardrop.routers.auth.settings.allow_public_registration", True)
    monkeypatch.setattr("teardrop.routers.auth.settings.turnstile_secret_key", "ts_secret")
    verify_turnstile = AsyncMock(return_value=True)
    monkeypatch.setattr("teardrop.routers.auth.verify_turnstile", verify_turnstile)
    monkeypatch.setattr("teardrop.routers.auth.register_org_and_user", AsyncMock(return_value=(org, user)))
    monkeypatch.setattr("teardrop.routers.auth.create_verification_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr("teardrop.routers.auth.send_verification_email", AsyncMock())
    monkeypatch.setattr("teardrop.routers.auth.create_refresh_token", AsyncMock(return_value="rt"))

    resp = await anon_client.post(
        "/register",
        json={
            "org_name": "Org",
            "email": "alice@example.com",
            "password": "strongpass1",
            "captcha_token": "cf-token",
        },
    )

    assert resp.status_code == 201
    verify_turnstile.assert_awaited_once()
