"""API tests for POST /org/credentials/regenerate (admin-only rotation)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from teardrop.users import OrgClientCredential


def _mock_cred(org_id: str = "test-org-id") -> OrgClientCredential:
    return OrgClientCredential(
        client_id="client-new",
        org_id=org_id,
        hashed_secret="hashed",
        salt="salt",
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.anyio
async def test_regenerate_credentials_admin_success(admin_api_client, monkeypatch):
    delete_mock = AsyncMock()
    create_mock = AsyncMock(return_value=(_mock_cred(), "plaintext-secret"))
    monkeypatch.setattr("teardrop.routers.auth.delete_org_client_credentials", delete_mock)
    monkeypatch.setattr("teardrop.routers.auth.create_client_credential", create_mock)

    resp = await admin_api_client.post("/org/credentials/regenerate")

    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"] == "client-new"
    assert body["client_secret"] == "plaintext-secret"
    delete_mock.assert_awaited_once()
    create_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_regenerate_credentials_member_forbidden(api_client, monkeypatch):
    """Non-admin members must not be able to rotate (destroy) org credentials."""
    delete_mock = AsyncMock()
    create_mock = AsyncMock(return_value=(_mock_cred(), "plaintext-secret"))
    monkeypatch.setattr("teardrop.routers.auth.delete_org_client_credentials", delete_mock)
    monkeypatch.setattr("teardrop.routers.auth.create_client_credential", create_mock)

    resp = await api_client.post("/org/credentials/regenerate")

    assert resp.status_code == 403
    delete_mock.assert_not_awaited()
    create_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_regenerate_credentials_no_auth_401(anon_client):
    resp = await anon_client.post("/org/credentials/regenerate")
    assert resp.status_code == 401
