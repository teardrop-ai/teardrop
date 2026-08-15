"""Unit tests for the DeBank-backed wallet positions tool."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

positions_module = importlib.import_module("tools.definitions.get_wallet_positions")
debank_module = importlib.import_module("tools._internals._debank")

_WALLET = "0x0000000000000000000000000000000000000001"


def _response(status: int, payload: object) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=payload)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


def _mock_session(*responses: MagicMock) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(side_effect=list(responses))
    return session


def _protocol_payload() -> list[dict]:
    return [
        {
            "id": "aave-v3",
            "chain": "arb",
            "name": "Aave V3",
            "site_url": "https://aave.com",
            "has_supported_portfolio": True,
            "tvl": 1_000_000_000,
            "portfolio_item_list": [
                {
                    "name": "Lending",
                    "update_at": 1_700_000_000.0,
                    "stats": {
                        "asset_usd_value": 125.5,
                        "debt_usd_value": 25.5,
                        "net_usd_value": 100.0,
                    },
                    "detail_types": ["common"],
                    "detail": {
                        "supply_token_list": [
                            {
                                "id": "0x1111111111111111111111111111111111111111",
                                "chain": "arb",
                                "symbol": "USDC",
                                "optimized_symbol": "USDC.e",
                                "decimals": 6,
                                "amount": 100.0,
                                "price": 1.0,
                                "protocol_id": "aave-v3",
                                "is_verified": True,
                            }
                        ],
                        "reward_token_list": [],
                    },
                }
            ],
        }
    ]


def _balance_payload() -> dict:
    return {
        "total_usd_value": 321.25,
        "chain_list": [
            {"id": "arb", "name": "Arbitrum", "community_id": 42161, "usd_value": 200.0},
            {"id": "base", "name": "Base", "community_id": 8453, "usd_value": 121.25},
        ],
    }


@pytest.fixture(autouse=True)
def _clear_debank_cache() -> None:
    debank_module.clear_wallet_cache()


def test_input_normalizes_wallet_address() -> None:
    result = positions_module.GetWalletPositionsInput(wallet_address=_WALLET.lower())
    assert result.wallet_address == _WALLET
    assert result.include_net_worth is True


def test_input_rejects_non_evm_address() -> None:
    with pytest.raises(ValidationError):
        positions_module.GetWalletPositionsInput(wallet_address="not-an-address")


@pytest.mark.asyncio
async def test_missing_api_key_returns_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(debank_module, "get_settings", lambda: SimpleNamespace(debank_api_key=""))
    session = MagicMock()
    session.get = MagicMock()
    monkeypatch.setattr(debank_module, "get_debank_session", AsyncMock(return_value=session))

    result = await positions_module.get_wallet_positions(_WALLET)

    assert result["data_complete"] is False
    assert result["errors"][0]["error_type"] == "configuration"
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_normalizes_positions_and_net_worth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(debank_module, "get_settings", lambda: SimpleNamespace(debank_api_key="test-key"))
    session = _mock_session(_response(200, _protocol_payload()), _response(200, _balance_payload()))
    monkeypatch.setattr(debank_module, "get_debank_session", AsyncMock(return_value=session))

    result = await positions_module.get_wallet_positions(_WALLET)

    assert result["data_complete"] is True
    assert result["total_net_worth_usd"] == 321.25
    assert result["chain_balances"][0]["chain_id"] == "arb"
    assert result["positions"][0]["protocol_id"] == "aave-v3"
    item = result["positions"][0]["items"][0]
    token = item["token_lists"]["supply_token_list"][0]
    assert item["net_usd_value"] == 100.0
    assert token["display_symbol"] == "USDC.e"
    assert token["usd_value"] == 100.0
    assert result["provenance"]["provider"] == "DeBank Cloud"
    assert result["provenance"]["cache_hit"] is False
    assert session.get.call_count == 2


@pytest.mark.asyncio
async def test_staleness_seconds_from_update_at(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(debank_module, "get_settings", lambda: SimpleNamespace(debank_api_key="test-key"))
    # update_at is 3600s in the past → staleness_seconds should be ~3600.
    payload = _protocol_payload()
    payload[0]["portfolio_item_list"][0]["update_at"] = 1_700_000_000.0
    session = _mock_session(_response(200, payload), _response(200, _balance_payload()))
    monkeypatch.setattr(debank_module, "get_debank_session", AsyncMock(return_value=session))

    result = await positions_module.get_wallet_positions(_WALLET)

    assert result["staleness_seconds"] is not None
    assert result["staleness_seconds"] >= 0
    assert result["positions"][0]["items"][0]["update_at"] == 1_700_000_000


@pytest.mark.asyncio
async def test_staleness_none_when_no_update_at(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(debank_module, "get_settings", lambda: SimpleNamespace(debank_api_key="test-key"))
    payload = _protocol_payload()
    payload[0]["portfolio_item_list"][0]["update_at"] = None
    session = _mock_session(_response(200, payload), _response(200, _balance_payload()))
    monkeypatch.setattr(debank_module, "get_debank_session", AsyncMock(return_value=session))

    result = await positions_module.get_wallet_positions(_WALLET)

    assert result["staleness_seconds"] is None


@pytest.mark.asyncio
async def test_successful_response_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(debank_module, "get_settings", lambda: SimpleNamespace(debank_api_key="test-key"))
    session = _mock_session(_response(200, _protocol_payload()), _response(200, _balance_payload()))
    monkeypatch.setattr(debank_module, "get_debank_session", AsyncMock(return_value=session))

    first = await positions_module.get_wallet_positions(_WALLET)
    second = await positions_module.get_wallet_positions(_WALLET)

    assert first["provenance"]["cache_hit"] is False
    assert second["provenance"]["cache_hit"] is True
    assert session.get.call_count == 2


@pytest.mark.asyncio
async def test_partial_upstream_failure_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(debank_module, "get_settings", lambda: SimpleNamespace(debank_api_key="test-key"))
    session = _mock_session(_response(200, _protocol_payload()), _response(500, {}))
    monkeypatch.setattr(debank_module, "get_debank_session", AsyncMock(return_value=session))

    result = await positions_module.get_wallet_positions(_WALLET)

    assert result["data_complete"] is False
    assert result["total_net_worth_usd"] is None
    assert result["positions"][0]["protocol_id"] == "aave-v3"
    assert result["errors"] == [
        {
            "operation": "total_balance",
            "error_type": "upstream_error",
            "message": "DeBank total_balance returned HTTP 500",
        }
    ]
    assert result["provenance"]["source_fetched_at"] is None


@pytest.mark.asyncio
async def test_positions_only_mode_makes_one_provider_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(debank_module, "get_settings", lambda: SimpleNamespace(debank_api_key="test-key"))
    session = _mock_session(_response(200, _protocol_payload()))
    monkeypatch.setattr(debank_module, "get_debank_session", AsyncMock(return_value=session))

    result = await positions_module.get_wallet_positions(_WALLET, include_net_worth=False)

    assert result["include_net_worth"] is False
    assert result["total_net_worth_usd"] is None
    assert result["chain_balances"] == []
    assert result["data_complete"] is True
    session.get.assert_called_once()
    assert session.get.call_args.args[0].endswith("/user/all_complex_protocol_list")


@pytest.mark.asyncio
async def test_token_balance_mode_adds_one_provider_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(debank_module, "get_settings", lambda: SimpleNamespace(debank_api_key="test-key"))
    token_payload = [
        {
            "id": "0x1111111111111111111111111111111111111111",
            "chain": "eth",
            "symbol": "USDC",
            "amount": 12.5,
            "price": 1.0,
            "decimals": 6,
            "raw_amount": 12500000,
        }
    ]
    session = _mock_session(_response(200, _protocol_payload()), _response(200, token_payload))
    monkeypatch.setattr(debank_module, "get_debank_session", AsyncMock(return_value=session))

    result = await positions_module.get_wallet_positions(_WALLET, include_net_worth=False, include_token_balances=True)

    assert result["include_token_balances"] is True
    assert result["token_balances"][0]["symbol"] == "USDC"
    assert result["token_balances"][0]["usd_value"] == 12.5
    assert session.get.call_count == 2
    assert "/user/all_token_list?id=" in session.get.call_args_list[1].args[0]


@pytest.mark.asyncio
async def test_endpoint_requests_are_cached_by_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(debank_module, "get_settings", lambda: SimpleNamespace(debank_api_key="test-key"))
    session = _mock_session(_response(200, {"history_list": []}))
    monkeypatch.setattr(debank_module, "get_debank_session", AsyncMock(return_value=session))

    first = await debank_module.get_wallet_history(_WALLET, chain_ids=["eth"], page_count=20)
    second = await debank_module.get_wallet_history(_WALLET, chain_ids=["eth"], page_count=20)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert session.get.call_count == 1
    request_url = session.get.call_args.args[0]
    assert "/user/all_history_list?" in request_url
    assert "chain_ids=eth" in request_url
    assert "page_count=20" in request_url
