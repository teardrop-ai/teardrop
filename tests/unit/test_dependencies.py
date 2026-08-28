# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from teardrop.dependencies import require_settlement_wallet_auth


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
