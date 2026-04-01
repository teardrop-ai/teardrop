"""API tests for POST /admin/orgs and POST /admin/users endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from users import Org, User


@pytest.mark.anyio
async def test_admin_create_org(admin_api_client, monkeypatch):
    mock_org = Org(id="org-new", name="New Org", created_at=datetime.now(timezone.utc))
    monkeypatch.setattr("app.create_org", AsyncMock(return_value=mock_org))

    resp = await admin_api_client.post("/admin/orgs", json={"name": "New Org"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "New Org"
    assert "id" in body


@pytest.mark.anyio
async def test_admin_create_org_requires_admin(api_client, monkeypatch):
    """Regular user (role=user) must receive 403."""
    resp = await api_client.post("/admin/orgs", json={"name": "Sneaky Org"})
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_admin_create_org_no_auth(anon_client):
    resp = await anon_client.post("/admin/orgs", json={"name": "No Auth Org"})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_admin_create_user(admin_api_client, monkeypatch):
    mock_user = User(
        id="user-new",
        email="newuser@example.com",
        org_id="org-123",
        hashed_secret="hashed",
        salt="salt",
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr("app.create_user", AsyncMock(return_value=mock_user))

    resp = await admin_api_client.post(
        "/admin/users",
        json={
            "email": "newuser@example.com",
            "secret": "strongpass123",
            "org_id": "org-123",
            "role": "user",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "newuser@example.com"
    assert "id" in body


@pytest.mark.anyio
async def test_admin_create_user_requires_admin(api_client):
    resp = await api_client.post(
        "/admin/users",
        json={
            "email": "evil@example.com",
            "secret": "pass123456",
            "org_id": "org-1",
        },
    )
    assert resp.status_code == 403
