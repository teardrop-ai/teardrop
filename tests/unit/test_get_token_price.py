"""Unit tests for tools/definitions/get_token_price.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from tools.definitions.get_token_price import (
    GetTokenPriceInput,
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


class TestGetTokenPrice:
    async def test_returns_prices(self, test_settings, monkeypatch):
        # Clear cache
        monkeypatch.setattr("tools.definitions.get_token_price._price_cache", {})
        monkeypatch.setattr("tools.definitions.get_token_price._price_cache_expires", 0.0)

        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "bitcoin": {
                "usd": 65000.0,
                "usd_market_cap": 1_200_000_000_000,
                "usd_24h_vol": 30_000_000_000,
                "usd_24h_change": 2.5,
            },
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_token_price.aiohttp.ClientSession", return_value=mock_session):
            result = await get_token_price(tokens=["BTC"])

        assert result["vs_currency"] == "usd"
        assert len(result["prices"]) == 1
        assert result["prices"][0]["price"] == 65000.0
        assert result["prices"][0]["symbol"] == "BTC"

    async def test_api_failure_returns_none_prices(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price._price_cache", {})
        monkeypatch.setattr("tools.definitions.get_token_price._price_cache_expires", 0.0)

        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 429  # Rate limited
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_token_price.aiohttp.ClientSession", return_value=mock_session):
            result = await get_token_price(tokens=["ETH"])

        assert result["prices"][0]["price"] is None
