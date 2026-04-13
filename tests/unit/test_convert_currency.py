"""Unit tests for tools/definitions/convert_currency.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from tools.definitions.convert_currency import (
    _CRYPTO_IDS,
    _FIAT_CODES,
    ConvertCurrencyInput,
    convert_currency,
)


class TestConvertCurrencyInput:
    def test_valid_input(self):
        inp = ConvertCurrencyInput(amount=100.0, from_currency="USD", to_currency="EUR")
        assert inp.amount == 100.0

    def test_negative_amount_rejected(self):
        with pytest.raises(ValidationError):
            ConvertCurrencyInput(amount=-1, from_currency="USD", to_currency="EUR")


class TestConvertCurrency:
    async def test_fiat_to_fiat_uses_fallback_when_api_fails(self, test_settings, monkeypatch):
        # Zero out caches
        monkeypatch.setattr("tools.definitions.convert_currency._fiat_cache", {})
        monkeypatch.setattr("tools.definitions.convert_currency._fiat_cache_expires", 0.0)

        # Simulate API failure so fallback rates are used
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.convert_currency.aiohttp.ClientSession", return_value=mock_session):  # noqa: E501
            result = await convert_currency(amount=100.0, from_currency="USD", to_currency="EUR")

        assert result["from_currency"] == "USD"
        assert result["to_currency"] == "EUR"
        assert result["converted_amount"] > 0

    async def test_same_currency_returns_same_amount(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.convert_currency._fiat_cache", {})
        monkeypatch.setattr("tools.definitions.convert_currency._fiat_cache_expires", 0.0)

        result = await convert_currency(amount=42.0, from_currency="USD", to_currency="USD")
        assert result["converted_amount"] == 42.0
        assert result["rate"] == 1.0

    async def test_crypto_ids_mapping(self):
        assert "btc" in _CRYPTO_IDS
        assert "eth" in _CRYPTO_IDS
        assert _CRYPTO_IDS["btc"] == "bitcoin"

    async def test_fiat_codes_set(self):
        assert "usd" in _FIAT_CODES
        assert "eur" in _FIAT_CODES
        assert "gbp" in _FIAT_CODES
