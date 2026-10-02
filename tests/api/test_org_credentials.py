# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""API tests for POST /org/credentials/regenerate."""

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
async def test_regenerate_credentials_honors_requested_scope(admin_api_client, monkeypatch):
    cred = OrgClientCredential(**{**_mock_cred().model_dump(), "scope": "read"})
    create_mock = AsyncMock(return_value=(cred, "plaintext-secret"))
    monkeypatch.setattr("teardrop.routers.auth.delete_org_client_credentials", AsyncMock())
    monkeypatch.setattr("teardrop.routers.auth.create_client_credential", create_mock)

    resp = await admin_api_client.post("/org/credentials/regenerate?scope=read")

    assert resp.status_code == 201
    assert resp.json()["scope"] == "read"
    create_mock.assert_awaited_once_with("test-org-id", scope="read")


@pytest.mark.anyio
async def test_regenerate_credentials_rejects_unknown_scope(admin_api_client):
    resp = await admin_api_client.post("/org/credentials/regenerate?scope=admin")
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_regenerate_credentials_member_forbidden(api_client, monkeypatch):
    """Non-SIWE members must not be able to rotate org credentials."""
    delete_mock = AsyncMock()
    create_mock = AsyncMock(return_value=(_mock_cred(), "plaintext-secret"))
    monkeypatch.setattr("teardrop.routers.auth.delete_org_client_credentials", delete_mock)
    monkeypatch.setattr("teardrop.routers.auth.create_client_credential", create_mock)

    resp = await api_client.post("/org/credentials/regenerate")

    assert resp.status_code == 403
    delete_mock.assert_not_awaited()
    create_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_regenerate_credentials_machine_siwe_owner_success(api_client, monkeypatch):
    from teardrop.auth import require_auth
    from teardrop.main import app
    from teardrop.users import Org
    from teardrop.wallets import Wallet

    async def siwe_owner_auth():
        return {
            "sub": "siwe-user",
            "role": "user",
            "org_id": "machine-org",
            "auth_method": "siwe",
            "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "chain_id": 84532,
        }

    app.dependency_overrides[require_auth] = siwe_owner_auth
    monkeypatch.setattr(
        "teardrop.users.get_org_by_id",
        AsyncMock(
            return_value=Org(
                id="machine-org",
                name="machine",
                acquisition_source="x402",
                created_at=datetime.now(timezone.utc),
            )
        ),
    )
    monkeypatch.setattr(
        "teardrop.wallets.get_wallet_by_address",
        AsyncMock(
            return_value=Wallet(
                id="wallet-1",
                address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
                chain_id=84532,
                user_id="siwe-user",
                org_id="machine-org",
                is_primary=True,
                created_at=datetime.now(timezone.utc),
            )
        ),
    )
    delete_mock = AsyncMock()
    create_mock = AsyncMock(return_value=(_mock_cred("machine-org"), "plaintext-secret"))
    monkeypatch.setattr("teardrop.routers.auth.delete_org_client_credentials", delete_mock)
    monkeypatch.setattr("teardrop.routers.auth.create_client_credential", create_mock)

    resp = await api_client.post("/org/credentials/regenerate")

    assert resp.status_code == 201
    assert resp.json()["client_secret"] == "plaintext-secret"
    delete_mock.assert_awaited_once_with("machine-org")
    create_mock.assert_awaited_once_with("machine-org", scope="publish")


@pytest.mark.anyio
async def test_regenerate_credentials_rejects_non_owner_siwe_wallet(api_client, monkeypatch):
    from teardrop.auth import require_auth
    from teardrop.main import app
    from teardrop.users import Org
    from teardrop.wallets import Wallet

    async def siwe_auth():
        return {
            "sub": "siwe-user",
            "role": "user",
            "org_id": "machine-org",
            "auth_method": "siwe",
            "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "chain_id": 84532,
        }

    app.dependency_overrides[require_auth] = siwe_auth
    monkeypatch.setattr(
        "teardrop.users.get_org_by_id",
        AsyncMock(
            return_value=Org(
                id="machine-org",
                name="machine",
                acquisition_source="x402",
                created_at=datetime.now(timezone.utc),
            )
        ),
    )
    monkeypatch.setattr(
        "teardrop.wallets.get_wallet_by_address",
        AsyncMock(
            return_value=Wallet(
                id="wallet-1",
                address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
                chain_id=84532,
                user_id="different-user",
                org_id="machine-org",
                is_primary=True,
                created_at=datetime.now(timezone.utc),
            )
        ),
    )
    delete_mock = AsyncMock()
    create_mock = AsyncMock()
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


# ─── P0.3: scope listing + per-credential disable ────────────────────────────


def _cred(client_id: str, *, scope: str = "publish", disabled_at: datetime | None = None) -> OrgClientCredential:
    return OrgClientCredential(
        client_id=client_id,
        org_id="test-org-id",
        hashed_secret="hashed",
        salt="salt",
        created_at=datetime.now(timezone.utc),
        scope=scope,
        disabled_at=disabled_at,
    )


@pytest.mark.anyio
async def test_get_credentials_exposes_scope_and_disabled_at(api_client, monkeypatch):
    now = datetime.now(timezone.utc)
    creds = [_cred("cred-active", scope="publish"), _cred("cred-disabled", scope="read", disabled_at=now)]
    monkeypatch.setattr("teardrop.routers.auth.list_org_client_credentials", AsyncMock(return_value=creds))

    resp = await api_client.get("/org/credentials")

    assert resp.status_code == 200
    items = {i["client_id"]: i for i in resp.json()}
    assert items["cred-active"]["scope"] == "publish"
    assert items["cred-active"]["disabled_at"] is None
    assert items["cred-disabled"]["scope"] == "read"
    assert items["cred-disabled"]["disabled_at"] == now.isoformat()


@pytest.mark.anyio
async def test_disable_credential_admin_success(admin_api_client, monkeypatch):
    now = datetime.now(timezone.utc)
    disable_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("teardrop.routers.auth.disable_client_credential", disable_mock)
    monkeypatch.setattr(
        "teardrop.routers.auth.list_org_client_credentials",
        AsyncMock(return_value=[_cred("cred-1", disabled_at=now)]),
    )

    resp = await admin_api_client.post("/org/credentials/cred-1/disable")

    assert resp.status_code == 200
    assert resp.json() == {"client_id": "cred-1", "disabled_at": now.isoformat()}
    disable_mock.assert_awaited_once_with("cred-1", "test-org-id")


@pytest.mark.anyio
async def test_disable_credential_unknown_returns_404(admin_api_client, monkeypatch):
    disable_mock = AsyncMock(return_value=False)
    monkeypatch.setattr("teardrop.routers.auth.disable_client_credential", disable_mock)
    monkeypatch.setattr("teardrop.routers.auth.list_org_client_credentials", AsyncMock(return_value=[]))

    resp = await admin_api_client.post("/org/credentials/unknown/disable")

    assert resp.status_code == 404


@pytest.mark.anyio
async def test_disable_credential_member_forbidden(api_client, monkeypatch):
    disable_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.auth.disable_client_credential", disable_mock)

    resp = await api_client.post("/org/credentials/cred-1/disable")

    assert resp.status_code == 403
    disable_mock.assert_not_awaited()
