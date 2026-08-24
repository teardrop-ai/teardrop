# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Tests for the DeBank-backed wallet approvals tool."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

module = __import__("tools.definitions.get_wallet_approvals", fromlist=["module"])

_WALLET = "0x0000000000000000000000000000000000000001"


def _snapshot(payload: object, *, error: object | None = None, cache_hit: bool = False):
    return module.DebankEndpointSnapshot(
        payload=payload,
        error=error,
        source_fetched_at="2026-08-15T00:00:00Z" if error is None else None,
        source_url="https://pro-openapi.debank.com/v1/user/token_authorized_list",
        cache_hit=cache_hit,
    )


def test_input_normalizes_wallet_and_chain() -> None:
    result = module.GetWalletApprovalsInput(wallet_address=_WALLET.lower(), chain_id=" ETH ")
    assert result.wallet_address == _WALLET
    assert result.chain_id == "eth"


def test_input_rejects_invalid_chain() -> None:
    with pytest.raises(ValidationError):
        module.GetWalletApprovalsInput(wallet_address=_WALLET, chain_id="eth?secret=true")


@pytest.mark.asyncio
async def test_normalizes_risk_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        {
            "id": "0x1111111111111111111111111111111111111111",
            "name": "USD Coin",
            "symbol": "USDC",
            "chain": "eth",
            "price": 1.0,
            "balance": 10.0,
            "spenders": [
                {
                    "id": "0x2222222222222222222222222222222222222222",
                    "value": "0xffff",
                    "exposure_usd": 10.0,
                    "protocol": {"id": "example", "name": "Example"},
                    "is_contract": True,
                    "is_open_source": False,
                    "is_hacked": True,
                    "is_abandoned": False,
                }
            ],
            "sum_exposure_usd": 10.0,
            "exposure_balance": 10.0,
        }
    ]
    monkeypatch.setattr(module, "fetch_wallet_approvals", AsyncMock(return_value=_snapshot(payload)))

    result = await module.get_wallet_approvals(_WALLET.lower(), "ETH")

    assert result["data_complete"] is True
    assert result["approvals"][0]["spenders"][0]["is_hacked"] is True
    assert result["approvals"][0]["spenders"][0]["value"] == "0xffff"
    assert result["provenance"]["provider"] == "DeBank Cloud"


@pytest.mark.asyncio
async def test_provider_error_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    error = SimpleNamespace(operation="token_authorized_list", error_type="configuration", message="missing key")
    monkeypatch.setattr(module, "fetch_wallet_approvals", AsyncMock(return_value=_snapshot(None, error=error)))

    result = await module.get_wallet_approvals(_WALLET, "eth")

    assert result["data_complete"] is False
    assert result["approvals"] == []
    assert result["errors"][0]["error_type"] == "configuration"
    assert result["provenance"]["source_fetched_at"] is None
