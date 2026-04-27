"""API tests for POST /org/invite and POST /register/invite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from users import OrgInvite, User


@pytest.fixture(autouse=True)
def _bypass_rate_limit(monkeypatch):
    monkeypatch.setattr("app._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))


def _mock_invite(
    email: str | None = None,
    role: str = "user",
    org_id: str = "org-1",
    used: bool = False,
    expired: bool = False,
) -> OrgInvite:
    now = datetime.now(timezone.utc)
    expires_at = now - timedelta(hours=1) if expired else now + timedelta(hours=72)
    return OrgInvite(
        token="invite-tok-abc",
        org_id=org_id,
        email=email,
        role=role,
        invited_by="user-admin",
        created_at=now,
        expires_at=expires_at,
        used=used,
    )


def _mock_user(org_id: str = "org-1") -> User:
    return User(
        id="user-invited",
        email="newbie@example.com",
        org_id=org_id,
        hashed_secret="hashed",
        salt="salt",
        role="user",
        is_active=True,
        is_verified=True,
        created_at=datetime.now(timezone.utc),
    )


# ─── POST /org/invite ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_invite_happy_path(api_client, monkeypatch):
    invite = _mock_invite(email="newbie@example.com")
    monkeypatch.setattr("app.create_org_invite", AsyncMock(return_value=invite))
    monkeypatch.setattr("app.send_invite_email", AsyncMock())

    resp = await api_client.post("/org/invite", json={"email": "newbie@example.com", "role": "user"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["token"] == invite.token
    assert "expires_at" in body


@pytest.mark.anyio
async def test_create_invite_no_auth_401(anon_client):
    resp = await anon_client.post("/org/invite", json={"email": "newbie@example.com", "role": "user"})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_create_invite_no_email(api_client, monkeypatch):
    """Invite without a pre-filled email should still succeed."""
    invite = _mock_invite(email=None)
    monkeypatch.setattr("app.create_org_invite", AsyncMock(return_value=invite))
    monkeypatch.setattr("app.send_invite_email", AsyncMock())

    resp = await api_client.post("/org/invite", json={"role": "user"})

    assert resp.status_code == 201


# ─── POST /register/invite ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_accept_invite_happy_path(anon_client, monkeypatch):
    invite = _mock_invite(email=None)
    user = _mock_user()
    monkeypatch.setattr("app.get_org_invite", AsyncMock(return_value=invite))
    monkeypatch.setattr("app.consume_org_invite", AsyncMock(return_value=True))
    monkeypatch.setattr("app.create_user", AsyncMock(return_value=user))
    monkeypatch.setattr("app.create_refresh_token", AsyncMock(return_value="rt-xyz"))

    resp = await anon_client.post(
        "/register/invite",
        json={"token": "invite-tok-abc", "email": "newbie@example.com", "password": "strongpass1"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert "access_token" in body
    assert body["refresh_token"] == "rt-xyz"


@pytest.mark.anyio
async def test_accept_invite_invalid_token_410(anon_client, monkeypatch):
    monkeypatch.setattr("app.get_org_invite", AsyncMock(return_value=None))

    resp = await anon_client.post(
        "/register/invite",
        json={"token": "bad-tok", "email": "x@x.com", "password": "strongpass1"},
    )

    assert resp.status_code == 410


@pytest.mark.anyio
async def test_accept_invite_race_condition_410(anon_client, monkeypatch):
    """If get_org_invite passes but consume_org_invite fails (race), return 410."""
    invite = _mock_invite(email=None)
    monkeypatch.setattr("app.get_org_invite", AsyncMock(return_value=invite))
    monkeypatch.setattr("app.consume_org_invite", AsyncMock(return_value=False))

    resp = await anon_client.post(
        "/register/invite",
        json={"token": "invite-tok-abc", "email": "x@x.com", "password": "strongpass1"},
    )

    assert resp.status_code == 410


@pytest.mark.anyio
async def test_accept_invite_email_mismatch_422(anon_client, monkeypatch):
    """Invite with pre-filled email must reject a different accepting email."""
    invite = _mock_invite(email="specific@example.com")
    monkeypatch.setattr("app.get_org_invite", AsyncMock(return_value=invite))

    resp = await anon_client.post(
        "/register/invite",
        json={"token": "invite-tok-abc", "email": "different@example.com", "password": "pass1234"},
    )

    assert resp.status_code == 422


@pytest.mark.anyio
async def test_accept_invite_email_match_case_insensitive(anon_client, monkeypatch):
    """Email match must be case-insensitive."""
    invite = _mock_invite(email="Specific@Example.com")
    user = _mock_user()
    monkeypatch.setattr("app.get_org_invite", AsyncMock(return_value=invite))
    monkeypatch.setattr("app.consume_org_invite", AsyncMock(return_value=True))
    monkeypatch.setattr("app.create_user", AsyncMock(return_value=user))
    monkeypatch.setattr("app.create_refresh_token", AsyncMock(return_value="rt"))

    resp = await anon_client.post(
        "/register/invite",
        json={
            "token": "invite-tok-abc",
            "email": "specific@example.com",
            "password": "strongpass1",
        },
    )

    assert resp.status_code == 201


@pytest.mark.anyio
async def test_accept_invite_no_email_restriction_any_email_ok(anon_client, monkeypatch):
    """Open invite (no email) must accept any email address."""
    invite = _mock_invite(email=None)
    user = _mock_user()
    monkeypatch.setattr("app.get_org_invite", AsyncMock(return_value=invite))
    monkeypatch.setattr("app.consume_org_invite", AsyncMock(return_value=True))
    monkeypatch.setattr("app.create_user", AsyncMock(return_value=user))
    monkeypatch.setattr("app.create_refresh_token", AsyncMock(return_value="rt"))

    resp = await anon_client.post(
        "/register/invite",
        json={
            "token": "invite-tok-abc",
            "email": "anyone@example.com",
            "password": "strongpass1",
        },
    )

    assert resp.status_code == 201


@pytest.mark.anyio
async def test_accept_invite_duplicate_email_409(anon_client, monkeypatch):
    import asyncpg

    invite = _mock_invite(email=None)
    monkeypatch.setattr("app.get_org_invite", AsyncMock(return_value=invite))
    monkeypatch.setattr("app.consume_org_invite", AsyncMock(return_value=True))
    monkeypatch.setattr("app.create_user", AsyncMock(side_effect=asyncpg.UniqueViolationError()))

    resp = await anon_client.post(
        "/register/invite",
        json={"token": "invite-tok-abc", "email": "dup@example.com", "password": "strongpass1"},
    )

    assert resp.status_code == 409


@pytest.mark.anyio
async def test_accept_invite_invited_user_is_verified(anon_client, monkeypatch):
    """User created via invite must have is_verified=True (invite is the trust signal)."""
    invite = _mock_invite(email=None)
    create_user_mock = AsyncMock(return_value=_mock_user())
    monkeypatch.setattr("app.get_org_invite", AsyncMock(return_value=invite))
    monkeypatch.setattr("app.consume_org_invite", AsyncMock(return_value=True))
    monkeypatch.setattr("app.create_user", create_user_mock)
    monkeypatch.setattr("app.create_refresh_token", AsyncMock(return_value="rt"))

    await anon_client.post(
        "/register/invite",
        json={"token": "invite-tok-abc", "email": "x@x.com", "password": "strongpass1"},
    )

    _, kwargs = create_user_mock.call_args
    assert kwargs.get("is_verified") is True
