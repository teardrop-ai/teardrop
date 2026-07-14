"""API tests for GET /auth/verify-email and POST /auth/resend-verification."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from teardrop.users import User


@pytest.fixture(autouse=True)
def _bypass_rate_limit(monkeypatch):
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))


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
    monkeypatch.setattr(
        "teardrop.routers.auth.verify_user_and_enqueue_onboarding_credit",
        AsyncMock(return_value=("user-123", "org-1", False)),
    )

    resp = await anon_client.get("/auth/verify-email?token=valid-token")

    assert resp.status_code == 200
    assert resp.json()["verified"] is True


@pytest.mark.anyio
async def test_verify_email_invalid_token(anon_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.auth.verify_user_and_enqueue_onboarding_credit",
        AsyncMock(return_value=(None, None, False)),
    )

    resp = await anon_client.get("/auth/verify-email?token=bad-token")

    assert resp.status_code == 410


@pytest.mark.anyio
async def test_verify_email_calls_verify_and_enqueue(anon_client, monkeypatch):
    verify_mock = AsyncMock(return_value=("user-abc", "org-1", False))
    monkeypatch.setattr("teardrop.routers.auth.verify_user_and_enqueue_onboarding_credit", verify_mock)

    await anon_client.get("/auth/verify-email?token=tok")

    verify_mock.assert_awaited_once()
    call_args = verify_mock.await_args.args
    assert call_args[0] == "tok"


@pytest.mark.anyio
async def test_verify_email_grants_onboarding_credit_when_enabled(anon_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.auth.settings.onboarding_credit_enabled", True)
    monkeypatch.setattr("teardrop.routers.auth.settings.onboarding_credit_usdc", 500_000)
    monkeypatch.setattr(
        "teardrop.routers.auth.verify_user_and_enqueue_onboarding_credit",
        AsyncMock(return_value=("user-123", "org-1", False)),
    )
    grant_mock = AsyncMock(return_value=500_000)
    monkeypatch.setattr("teardrop.routers.auth.grant_onboarding_credit", grant_mock)
    monkeypatch.setattr("teardrop.routers.auth.clear_onboarding_credit_outbox", AsyncMock())

    resp = await anon_client.get("/auth/verify-email?token=valid-token")

    assert resp.status_code == 200
    grant_mock.assert_awaited_once_with("org-1", 500_000)


@pytest.mark.anyio
async def test_verify_email_clears_outbox_after_immediate_grant(anon_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.auth.settings.onboarding_credit_enabled", True)
    monkeypatch.setattr("teardrop.routers.auth.settings.onboarding_credit_usdc", 500_000)
    monkeypatch.setattr(
        "teardrop.routers.auth.verify_user_and_enqueue_onboarding_credit",
        AsyncMock(return_value=("user-123", "org-1", False)),
    )
    monkeypatch.setattr("teardrop.routers.auth.grant_onboarding_credit", AsyncMock(return_value=500_000))
    clear_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.auth.clear_onboarding_credit_outbox", clear_mock)

    resp = await anon_client.get("/auth/verify-email?token=valid-token")

    assert resp.status_code == 200
    clear_mock.assert_awaited_once_with("org-1")


@pytest.mark.anyio
async def test_verify_email_succeeds_when_onboarding_grant_fails(anon_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.auth.settings.onboarding_credit_enabled", True)
    monkeypatch.setattr("teardrop.routers.auth.settings.onboarding_credit_usdc", 500_000)
    monkeypatch.setattr(
        "teardrop.routers.auth.verify_user_and_enqueue_onboarding_credit",
        AsyncMock(return_value=("user-123", "org-1", False)),
    )
    monkeypatch.setattr(
        "teardrop.routers.auth.grant_onboarding_credit",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    clear_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.auth.clear_onboarding_credit_outbox", clear_mock)

    resp = await anon_client.get("/auth/verify-email?token=valid-token")

    assert resp.status_code == 200
    assert resp.json() == {"verified": True}
    # The durable outbox row (enqueued atomically during verification) is left
    # intact for the background retry worker -- it must not be cleared here.
    clear_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_verify_email_does_not_grant_when_feature_disabled(anon_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.auth.settings.onboarding_credit_enabled", False)
    monkeypatch.setattr(
        "teardrop.routers.auth.verify_user_and_enqueue_onboarding_credit",
        AsyncMock(return_value=("user-123", "org-1", False)),
    )
    grant_mock = AsyncMock(return_value=500_000)
    monkeypatch.setattr("teardrop.routers.auth.grant_onboarding_credit", grant_mock)

    resp = await anon_client.get("/auth/verify-email?token=valid-token")

    assert resp.status_code == 200
    grant_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_verify_email_grant_failure_is_sanitized(anon_client, monkeypatch, caplog):
    import logging

    monkeypatch.setattr("teardrop.routers.auth.settings.onboarding_credit_enabled", True)
    monkeypatch.setattr("teardrop.routers.auth.settings.onboarding_credit_usdc", 500_000)
    monkeypatch.setattr(
        "teardrop.routers.auth.verify_user_and_enqueue_onboarding_credit",
        AsyncMock(return_value=("user-123", "org-1", False)),
    )
    monkeypatch.setattr(
        "teardrop.routers.auth.grant_onboarding_credit",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    monkeypatch.setattr("teardrop.routers.auth.clear_onboarding_credit_outbox", AsyncMock())
    caplog.set_level(logging.WARNING)

    await anon_client.get("/auth/verify-email?token=valid-token")

    auth_logs = [rec for rec in caplog.records if rec.name == "teardrop.routers.auth"]
    assert any("Onboarding credit grant unavailable" in rec.message for rec in auth_logs)
    for rec in auth_logs:
        assert "database unavailable" not in rec.message
        assert "Traceback" not in rec.message


# ─── POST /auth/resend-verification ──────────────────────────────────────────


@pytest.mark.anyio
async def test_resend_verification_unverified_user(anon_client, monkeypatch):
    user = _mock_user(is_verified=False)
    monkeypatch.setattr("teardrop.routers.auth.get_user_by_email", AsyncMock(return_value=user))
    create_vt = AsyncMock(return_value="tok")
    monkeypatch.setattr("teardrop.routers.auth.create_verification_token", create_vt)
    monkeypatch.setattr("teardrop.routers.auth.send_verification_email", AsyncMock())

    resp = await anon_client.post("/auth/resend-verification", json={"email": "user@example.com"})

    assert resp.status_code == 200
    create_vt.assert_awaited_once_with(user.id)


@pytest.mark.anyio
async def test_resend_verification_already_verified_is_noop(anon_client, monkeypatch):
    """Verified users: resend should silently do nothing but still return 200."""
    user = _mock_user(is_verified=True)
    monkeypatch.setattr("teardrop.routers.auth.get_user_by_email", AsyncMock(return_value=user))
    create_vt = AsyncMock(return_value="tok")
    monkeypatch.setattr("teardrop.routers.auth.create_verification_token", create_vt)
    monkeypatch.setattr("teardrop.routers.auth.send_verification_email", AsyncMock())

    resp = await anon_client.post("/auth/resend-verification", json={"email": "user@example.com"})

    assert resp.status_code == 200
    create_vt.assert_not_awaited()


@pytest.mark.anyio
async def test_resend_verification_unknown_email_no_oracle(anon_client, monkeypatch):
    """Unknown email must still return 200 — no disclosure of account existence."""
    monkeypatch.setattr("teardrop.routers.auth.get_user_by_email", AsyncMock(return_value=None))

    resp = await anon_client.post("/auth/resend-verification", json={"email": "ghost@example.com"})

    assert resp.status_code == 200


# ─── /token gate when require_email_verification=True ────────────────────────


@pytest.mark.anyio
async def test_token_gate_blocks_unverified_user(anon_client, test_settings, monkeypatch):
    """When require_email_verification=True, unverified users must receive 403."""
    import teardrop.config as _config

    monkeypatch.setenv("REQUIRE_EMAIL_VERIFICATION", "true")
    _config.get_settings.cache_clear()
    import teardrop.routers.auth as _app

    monkeypatch.setattr(_app, "settings", _config.get_settings())

    user = _mock_user(is_verified=False)
    monkeypatch.setattr("teardrop.routers.auth.get_user_by_email", AsyncMock(return_value=user))
    monkeypatch.setattr("teardrop.routers.auth.verify_secret", lambda *a, **kw: True)

    resp = await anon_client.post("/token", json={"email": "user@example.com", "secret": "correctpass"})
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_token_gate_passes_verified_user(anon_client, test_settings, monkeypatch):
    """Verified user must still receive a token when the gate is enabled."""
    import teardrop.config as _config

    monkeypatch.setenv("REQUIRE_EMAIL_VERIFICATION", "true")
    _config.get_settings.cache_clear()
    import teardrop.routers.auth as _app

    monkeypatch.setattr(_app, "settings", _config.get_settings())

    user = _mock_user(is_verified=True)
    monkeypatch.setattr("teardrop.routers.auth.get_user_by_email", AsyncMock(return_value=user))
    monkeypatch.setattr("teardrop.routers.auth.verify_secret", lambda *a, **kw: True)
    monkeypatch.setattr("teardrop.routers.auth.create_refresh_token", AsyncMock(return_value="rt"))

    resp = await anon_client.post("/token", json={"email": "user@example.com", "secret": "correctpass"})
    assert resp.status_code == 200
