"""Tests for the DeBank-backed wallet history tool."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

module = __import__("tools.definitions.get_wallet_history", fromlist=["module"])

_WALLET = "0x0000000000000000000000000000000000000001"


def _snapshot(payload: object, *, error: object | None = None, cache_hit: bool = False):
    return module.DebankEndpointSnapshot(
        payload=payload,
        error=error,
        source_fetched_at="2026-08-15T00:00:00Z" if error is None else None,
        source_url="https://pro-openapi.debank.com/v1/user/all_history_list",
        cache_hit=cache_hit,
    )


def test_input_normalizes_and_bounds_pagination() -> None:
    result = module.GetWalletHistoryInput(
        wallet_address=_WALLET.lower(),
        chain_ids=[" ETH ", "eth", "Base"],
        page_count=2,
    )
    assert result.wallet_address == _WALLET
    assert result.chain_ids == ["eth", "base"]
    with pytest.raises(ValidationError):
        module.GetWalletHistoryInput(wallet_address=_WALLET, page_count=21)


@pytest.mark.asyncio
async def test_preserves_history_metadata_and_returns_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "history_list": [
            {"cate_id": "send", "time_at": 200, "tx": {"usd_gas_fee": 1.2}},
            {"cate_id": "approve", "time_at": 100, "tx": {"status": 1}},
        ],
        "project_dict": {"uniswap3": {"id": "uniswap3", "name": "Uniswap V3"}},
        "token_dict": {"eth": {"id": "eth", "symbol": "ETH"}},
        "cex_dict": {},
    }
    fetch = AsyncMock(return_value=_snapshot(payload))
    monkeypatch.setattr(module, "fetch_wallet_history", fetch)

    result = await module.get_wallet_history(_WALLET.lower(), ["ETH"], page_count=2)

    assert result["data_complete"] is True
    assert result["history_list"][0]["cate_id"] == "send"
    assert result["project_dict"]["uniswap3"]["name"] == "Uniswap V3"
    assert result["next_cursor"] == 100
    assert result["has_more"] is True
    fetch.assert_awaited_once_with(_WALLET, chain_ids=["eth"], start_time=None, page_count=2)


@pytest.mark.asyncio
async def test_provider_error_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    error = SimpleNamespace(operation="all_history_list", error_type="rate_limited", message="provider unavailable")
    monkeypatch.setattr(module, "fetch_wallet_history", AsyncMock(return_value=_snapshot(None, error=error)))

    result = await module.get_wallet_history(_WALLET, page_count=20)

    assert result["data_complete"] is False
    assert result["history_list"] == []
    assert result["errors"][0]["error_type"] == "rate_limited"
    assert result["provenance"]["source_fetched_at"] is None


@pytest.mark.asyncio
async def test_malformed_history_is_not_reported_as_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "fetch_wallet_history", AsyncMock(return_value=_snapshot({"history_list": "bad"})))

    result = await module.get_wallet_history(_WALLET)

    assert result["data_complete"] is False
    assert result["errors"][0]["error_type"] == "malformed_response"
