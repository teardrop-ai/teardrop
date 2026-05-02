"""Unit tests for tools/definitions/get_token_price.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from tools.definitions.get_token_price import (
    TOOL,
    GetTokenPriceInput,
    _load_coins_list_index,
    _resolve_id,
    get_token_price,
)


class TestGetTokenPriceInput:
    def test_valid_input(self):
        inp = GetTokenPriceInput(tokens=["BTC", "ETH"])
        assert len(inp.tokens) == 2

    def test_empty_tokens_rejected(self):
        with pytest.raises(ValidationError):
            GetTokenPriceInput(tokens=[])

    def test_too_many_tokens_rejected(self):
        with pytest.raises(ValidationError):
            GetTokenPriceInput(tokens=[f"t{i}" for i in range(51)])


class TestResolveId:
    def test_known_symbols(self):
        assert _resolve_id("BTC") == "bitcoin"
        assert _resolve_id("eth") == "ethereum"
        assert _resolve_id("USDC") == "usd-coin"

    def test_unknown_passes_through(self):
        assert _resolve_id("somecoin") == "somecoin"

    def test_address_like_token_passes_through(self):
        assert _resolve_id("0xabc123") == "0xabc123"


def test_tool_description_mentions_address_limitations():
    assert "Bare 0x contract addresses" in TOOL.description


class TestLoadCoinsListIndex:
    async def test_builds_symbol_and_name_index(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_index", {})
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_expires", 0.0)
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_lock", None)
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_cooldown_until", 0.0)

        coins_data = [{"id": "liquity", "symbol": "lqty", "name": "Liquity"}]
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=coins_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_token_price.get_coingecko_session", new=AsyncMock(return_value=mock_session)):  # noqa: E501
            result = await _load_coins_list_index()

        assert result["lqty"] == "liquity"  # symbol lookup
        assert result["liquity"] == "liquity"  # name lookup

    async def test_caches_result_for_24h(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_index", {})
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_expires", 0.0)
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_lock", None)
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_cooldown_until", 0.0)

        coins_data = [{"id": "liquity", "symbol": "lqty", "name": "Liquity"}]
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=coins_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_token_price.get_coingecko_session", new=AsyncMock(return_value=mock_session)):  # noqa: E501
            first = await _load_coins_list_index()
            second = await _load_coins_list_index()  # should hit fast path

        assert first == second
        assert mock_session.get.call_count == 1  # HTTP called only once

    async def test_api_failure_returns_empty_dict(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_index", {})
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_expires", 0.0)
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_lock", None)
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_cooldown_until", 0.0)

        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_token_price.get_coingecko_session", new=AsyncMock(return_value=mock_session)):  # noqa: E501
            result = await _load_coins_list_index()

        assert result == {}  # graceful degradation, no exception

    async def test_first_match_wins_for_duplicate_symbols(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_index", {})
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_expires", 0.0)
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_lock", None)
        monkeypatch.setattr("tools.definitions.get_token_price._coins_list_cooldown_until", 0.0)

        coins_data = [
            {"id": "token-a", "symbol": "dup", "name": "Token A"},
            {"id": "token-b", "symbol": "dup", "name": "Token B"},
        ]
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=coins_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_token_price.get_coingecko_session", new=AsyncMock(return_value=mock_session)):  # noqa: E501
            result = await _load_coins_list_index()

        assert result["dup"] == "token-a"  # first entry wins


class TestGetTokenPrice:
    async def test_returns_prices(self, test_settings, monkeypatch):
        # Clear cache
        monkeypatch.setattr("tools.definitions.get_token_price._token_cache", {})

        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "bitcoin": {
                    "usd": 65000.0,
                    "usd_market_cap": 1_200_000_000_000,
                    "usd_24h_vol": 30_000_000_000,
                    "usd_24h_change": 2.5,
                },
            }
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_token_price.get_coingecko_session", new=AsyncMock(return_value=mock_session)):  # noqa: E501
            result = await get_token_price(tokens=["BTC"])

        assert result["vs_currency"] == "usd"
        assert len(result["prices"]) == 1
        assert result["prices"][0]["price"] == 65000.0
        assert result["prices"][0]["symbol"] == "BTC"

    async def test_api_failure_returns_none_prices(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price._token_cache", {})

        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 429  # Rate limited
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_token_price.get_coingecko_session", new=AsyncMock(return_value=mock_session)):  # noqa: E501
            result = await get_token_price(tokens=["ETH"])

        assert result["prices"][0]["price"] is None

    async def test_resolves_unknown_symbol_via_coins_list(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price._token_cache", {})

        coin_map = {"lqty": "liquity", "liquity": "liquity"}
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "liquity": {
                    "usd": 2.45,
                    "usd_market_cap": 230_000_000,
                    "usd_24h_vol": 5_000_000,
                    "usd_24h_change": -1.2,
                },
            }
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_token_price._load_coins_list_index", new=AsyncMock(return_value=coin_map)):  # noqa: E501
            with patch("tools.definitions.get_token_price.get_coingecko_session", new=AsyncMock(return_value=mock_session)):  # noqa: E501
                result = await get_token_price(tokens=["LQTY"])

        assert result["prices"][0]["price"] == 2.45
        assert result["prices"][0]["symbol"] == "LQTY"
        assert result["prices"][0]["id"] == "liquity"

    async def test_name_based_resolution(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price._token_cache", {})

        coin_map = {"lqty": "liquity", "liquity": "liquity"}
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "liquity": {
                    "usd": 2.45,
                    "usd_market_cap": 230_000_000,
                    "usd_24h_vol": 5_000_000,
                    "usd_24h_change": -1.2,
                },
            }
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_token_price._load_coins_list_index", new=AsyncMock(return_value=coin_map)):  # noqa: E501
            with patch("tools.definitions.get_token_price.get_coingecko_session", new=AsyncMock(return_value=mock_session)):  # noqa: E501
                result = await get_token_price(tokens=["Liquity"])

        assert result["prices"][0]["price"] == 2.45
        assert result["prices"][0]["symbol"] == "LIQUITY"  # original.upper() convention
        assert result["prices"][0]["id"] == "liquity"

    async def test_known_symbols_skip_coins_list(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price._token_cache", {})

        mock_load = AsyncMock(return_value={})
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "bitcoin": {
                    "usd": 65000.0,
                    "usd_market_cap": 1_200_000_000_000,
                    "usd_24h_vol": 30_000_000_000,
                    "usd_24h_change": 2.5,
                },
            }
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_token_price._load_coins_list_index", mock_load):
            with patch("tools.definitions.get_token_price.get_coingecko_session", new=AsyncMock(return_value=mock_session)):  # noqa: E501
                result = await get_token_price(tokens=["BTC"])

        mock_load.assert_not_called()  # static map resolved it; coins list never fetched
        assert result["prices"][0]["symbol"] == "BTC"
        assert result["prices"][0]["price"] == 65000.0

    async def test_mixed_known_and_unknown_tokens(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price._token_cache", {})

        coin_map = {"lqty": "liquity", "liquity": "liquity"}
        mock_load = AsyncMock(return_value=coin_map)
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "bitcoin": {
                    "usd": 65000.0,
                    "usd_market_cap": 1_200_000_000_000,
                    "usd_24h_vol": 30_000_000_000,
                    "usd_24h_change": 2.5,
                },
                "liquity": {
                    "usd": 2.45,
                    "usd_market_cap": 230_000_000,
                    "usd_24h_vol": 5_000_000,
                    "usd_24h_change": -1.2,
                },
            }
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_token_price._load_coins_list_index", mock_load):
            with patch("tools.definitions.get_token_price.get_coingecko_session", new=AsyncMock(return_value=mock_session)):  # noqa: E501
                result = await get_token_price(tokens=["BTC", "LQTY"])

        mock_load.assert_called_once()  # coins list fetched exactly once for LQTY
        btc = next(p for p in result["prices"] if p["symbol"] == "BTC")
        lqty = next(p for p in result["prices"] if p["symbol"] == "LQTY")
        assert btc["price"] == 65000.0
        assert lqty["price"] == 2.45
        assert lqty["id"] == "liquity"
