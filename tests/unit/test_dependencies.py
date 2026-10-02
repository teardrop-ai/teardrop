# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from teardrop.dependencies import (
    require_machine_scope,
    require_org_machine,
    require_scope,
    require_settlement_wallet_auth,
)


def _siwe_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "sub": "wallet-user",
        "org_id": "machine-org",
        "role": "user",
        "auth_method": "siwe",
        "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        "chain_id": 1,
    }
    payload.update(overrides)
    return payload


@pytest.mark.anyio
async def test_settlement_wallet_auth_allows_org_admin():
    payload = await require_settlement_wallet_auth({"role": "admin", "org_id": "org-1"})

    assert payload["org_id"] == "org-1"


@pytest.mark.anyio
async def test_org_machine_auth_allows_org_admin():
    payload = await require_org_machine({"role": "admin", "org_id": "org-1"})

    assert payload["org_id"] == "org-1"


@pytest.mark.anyio
async def test_org_machine_auth_allows_org_bound_client_credentials():
    payload = await require_org_machine({"auth_method": "client_credentials", "org_id": "machine-org"})

    assert payload["org_id"] == "machine-org"


@pytest.mark.anyio
async def test_org_machine_auth_rejects_unscoped_client_credentials():
    with pytest.raises(HTTPException) as exc_info:
        await require_org_machine({"auth_method": "client_credentials", "org_id": ""})

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_org_machine_auth_rejects_regular_member():
    with pytest.raises(HTTPException) as exc_info:
        await require_org_machine({"role": "user", "org_id": "org-1"})

    assert exc_info.value.status_code == 403


# ─── require_scope (P0.3 machine credential scopes) ─────────────────────────


@pytest.mark.anyio
async def test_scope_allows_org_admin():
    payload = await require_scope("publish")({"role": "admin", "org_id": "org-1"})

    assert payload["org_id"] == "org-1"


@pytest.mark.anyio
async def test_scope_allows_machine_credential_with_grant(monkeypatch):
    import teardrop.users as users

    disabled = AsyncMock(return_value=False)
    monkeypatch.setattr(users, "is_client_credential_disabled", disabled)

    payload = await require_scope("publish")(
        {"auth_method": "client_credentials", "org_id": "org-1", "scope": "publish", "sub": "cred-1"}
    )

    assert payload["org_id"] == "org-1"
    disabled.assert_awaited_once_with("cred-1")


@pytest.mark.anyio
async def test_scope_denies_machine_credential_without_grant(monkeypatch):
    import teardrop.users as users

    disabled = AsyncMock(return_value=False)
    monkeypatch.setattr(users, "is_client_credential_disabled", disabled)

    with pytest.raises(HTTPException) as exc_info:
        await require_scope("publish")({"auth_method": "client_credentials", "org_id": "org-1", "scope": "read", "sub": "cred-1"})

    assert exc_info.value.status_code == 403
    disabled.assert_not_awaited()


@pytest.mark.anyio
async def test_scope_denies_machine_credential_without_scope_claim(monkeypatch):
    """Pre-scope JWTs (minted before migration 118) fail closed on publish paths."""
    with pytest.raises(HTTPException) as exc_info:
        await require_scope("publish")({"auth_method": "client_credentials", "org_id": "org-1", "sub": "cred-1"})

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_scope_denies_disabled_credential(monkeypatch):
    import teardrop.users as users

    disabled = AsyncMock(return_value=True)
    monkeypatch.setattr(users, "is_client_credential_disabled", disabled)

    with pytest.raises(HTTPException) as exc_info:
        await require_scope("publish")(
            {"auth_method": "client_credentials", "org_id": "org-1", "scope": "publish", "sub": "cred-1"}
        )

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_scope_denies_config_fallback_credential():
    with pytest.raises(HTTPException) as exc_info:
        await require_scope("publish")({"auth_method": "client_credentials", "org_id": "", "scope": "publish", "sub": "cfg"})

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_scope_denies_regular_member():
    with pytest.raises(HTTPException) as exc_info:
        await require_scope("publish")({"role": "user", "org_id": "org-1"})

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_scope_allows_admin_without_org():
    """Admin without org_id is a malformed token, not a scope denial."""
    with pytest.raises(HTTPException) as exc_info:
        await require_scope("publish")({"role": "admin"})

    assert exc_info.value.status_code == 400


# ─── require_machine_scope (human paths unchanged) ──────────────────────────


@pytest.mark.anyio
async def test_machine_scope_allows_human_member_unchanged():
    payload = await require_machine_scope("publish")({"role": "user", "org_id": "org-1"})

    assert payload["org_id"] == "org-1"


@pytest.mark.anyio
async def test_machine_scope_allows_admin_unchanged():
    payload = await require_machine_scope("publish")({"role": "admin", "org_id": "org-1"})

    assert payload["org_id"] == "org-1"


@pytest.mark.anyio
async def test_machine_scope_allows_machine_with_grant(monkeypatch):
    import teardrop.users as users

    monkeypatch.setattr(users, "is_client_credential_disabled", AsyncMock(return_value=False))
    payload = await require_machine_scope("publish")(
        {"auth_method": "client_credentials", "org_id": "org-1", "scope": "publish", "sub": "cred-1"}
    )

    assert payload["org_id"] == "org-1"


@pytest.mark.anyio
async def test_machine_scope_denies_machine_without_grant():
    with pytest.raises(HTTPException) as exc_info:
        await require_machine_scope("publish")(
            {"auth_method": "client_credentials", "org_id": "org-1", "scope": "read", "sub": "cred-1"}
        )

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_machine_scope_denies_disabled_machine(monkeypatch):
    import teardrop.users as users

    monkeypatch.setattr(users, "is_client_credential_disabled", AsyncMock(return_value=True))
    with pytest.raises(HTTPException) as exc_info:
        await require_machine_scope("publish")(
            {"auth_method": "client_credentials", "org_id": "org-1", "scope": "publish", "sub": "cred-1"}
        )

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_machine_scope_denies_config_fallback():
    with pytest.raises(HTTPException) as exc_info:
        await require_machine_scope("publish")(
            {"auth_method": "client_credentials", "org_id": "", "scope": "publish", "sub": "cfg"}
        )

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_settlement_wallet_auth_rejects_admin_without_org():
    with pytest.raises(HTTPException) as exc_info:
        await require_settlement_wallet_auth({"role": "admin"})

    assert exc_info.value.status_code == 400


@pytest.mark.anyio
async def test_settlement_wallet_auth_allows_machine_org_siwe_owner(monkeypatch):
    monkeypatch.setattr(
        "teardrop.users.get_org_by_id",
        _async_value(SimpleNamespace(acquisition_source="siwe")),
    )
    monkeypatch.setattr(
        "teardrop.wallets.get_wallet_by_address",
        _async_value(SimpleNamespace(org_id="machine-org", user_id="wallet-user")),
    )

    payload = await require_settlement_wallet_auth(_siwe_payload())

    assert payload["auth_method"] == "siwe"


@pytest.mark.anyio
async def test_settlement_wallet_auth_rejects_client_credentials():
    with pytest.raises(HTTPException) as exc_info:
        await require_settlement_wallet_auth(_siwe_payload(auth_method="client_credentials", address=None, chain_id=None))

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_settlement_wallet_auth_rejects_missing_chain_id():
    with pytest.raises(HTTPException) as exc_info:
        await require_settlement_wallet_auth(_siwe_payload(chain_id=None))

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_settlement_wallet_auth_rejects_non_machine_org(monkeypatch):
    monkeypatch.setattr(
        "teardrop.users.get_org_by_id",
        _async_value(SimpleNamespace(acquisition_source="email")),
    )
    monkeypatch.setattr(
        "teardrop.wallets.get_wallet_by_address",
        _async_value(SimpleNamespace(org_id="machine-org", user_id="wallet-user")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await require_settlement_wallet_auth(_siwe_payload())

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_settlement_wallet_auth_rejects_wallet_from_other_org(monkeypatch):
    monkeypatch.setattr(
        "teardrop.users.get_org_by_id",
        _async_value(SimpleNamespace(acquisition_source="x402")),
    )
    monkeypatch.setattr(
        "teardrop.wallets.get_wallet_by_address",
        _async_value(SimpleNamespace(org_id="other-org", user_id="wallet-user")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await require_settlement_wallet_auth(_siwe_payload())

    assert exc_info.value.status_code == 403


def _async_value(value: object):
    async def _return_value(*args: object, **kwargs: object) -> object:
        return value

    return _return_value
