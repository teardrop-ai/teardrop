"""Unit tests for tools/definitions/get_token_price_historical.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from tools.definitions.get_token_price_historical import (
    GetTokenPriceHistoricalInput,
    _downsample_to_daily,
    _summarize,
    get_token_price_historical,
)


def _make_mock_session(status: int, json_payload: dict | None = None) -> MagicMock:
    """Build a mock aiohttp.ClientSession returning the given status/payload."""
    mock_session = MagicMock()
    mock_resp = AsyncMock()
    mock_resp.status = status
    if json_payload is not None:
        mock_resp.json = AsyncMock(return_value=json_payload)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


class TestGetTokenPriceHistoricalInput:
    def test_valid_minimal(self):
        inp = GetTokenPriceHistoricalInput(tokens=["ETH"], days=30)
        assert inp.days == 30
        assert inp.vs_currency == "usd"

    def test_empty_tokens_rejected(self):
        with pytest.raises(ValidationError):
            GetTokenPriceHistoricalInput(tokens=[])

    def test_too_many_tokens_rejected(self):
        with pytest.raises(ValidationError):
            GetTokenPriceHistoricalInput(tokens=[f"t{i}" for i in range(11)])

    def test_days_too_low_rejected(self):
        with pytest.raises(ValidationError):
            GetTokenPriceHistoricalInput(tokens=["ETH"], days=0)

    def test_days_too_high_rejected(self):
        with pytest.raises(ValidationError):
            GetTokenPriceHistoricalInput(tokens=["ETH"], days=366)

    def test_vs_currency_injection_rejected(self):
        with pytest.raises(ValidationError):
            GetTokenPriceHistoricalInput(tokens=["ETH"], vs_currency="usd; DROP TABLE")

    def test_vs_currency_uppercase_rejected(self):
        # Pattern requires lowercase to prevent ambiguity; agent should normalize.
        with pytest.raises(ValidationError):
            GetTokenPriceHistoricalInput(tokens=["ETH"], vs_currency="USD")

    def test_stats_only_default_false(self):
        inp = GetTokenPriceHistoricalInput(tokens=["ETH"])
        assert inp.stats_only is False


class TestDownsampleToDaily:
    def test_groups_by_day_keeps_last(self):
        # Two timestamps on the same UTC day; later price should win.
        ts1 = 1_700_000_000_000  # 2023-11-14 22:13:20 UTC
        ts2 = ts1 + 3_600_000  # +1 hour, same UTC day
        result = _downsample_to_daily([[ts1, 100.0], [ts2, 110.0]])
        assert len(result) == 1
        assert result[0]["price"] == 110.0

    def test_caps_at_90_entries(self):
        # 200 distinct UTC days → keep only the most-recent 90.
        day_ms = 86_400_000
        prices = [[1_600_000_000_000 + i * day_ms, float(i)] for i in range(200)]
        result = _downsample_to_daily(prices)
        assert len(result) == 90
        assert result[-1]["price"] == 199.0

    def test_iso_date_format(self):
        # 2024-01-01 00:00:00 UTC = 1704067200000 ms
        result = _downsample_to_daily([[1_704_067_200_000, 42.0]])
        assert result[0]["date"] == "2024-01-01"

    def test_malformed_entries_skipped(self):
        result = _downsample_to_daily([[1_704_067_200_000, 42.0], "garbage", []])  # type: ignore[list-item]
        assert len(result) == 1


class TestSummarize:
    def test_empty_input(self):
        s = _summarize(None)
        assert s["price_start"] is None
        assert s["daily_prices"] == []

    def test_computes_stats(self):
        prices = [[1_704_067_200_000, 100.0], [1_704_153_600_000, 110.0], [1_704_240_000_000, 90.0]]
        s = _summarize(prices)
        assert s["price_start"] == 100.0
        assert s["price_end"] == 90.0
        assert s["price_change_pct"] == pytest.approx(-10.0)
        assert s["price_high"] == 110.0
        assert s["price_low"] == 90.0


class TestGetTokenPriceHistorical:
    async def test_returns_price_stats(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price_historical._historical_cache", {})
        payload = {
            "prices": [
                [1_704_067_200_000, 2000.0],
                [1_704_153_600_000, 2100.0],
                [1_704_240_000_000, 2200.0],
            ]
        }
        session = _make_mock_session(200, payload)
        with patch(
            "tools.definitions.get_token_price_historical.aiohttp.ClientSession",
            return_value=session,
        ):
            result = await get_token_price_historical(tokens=["ETH"], days=7)

        assert result["vs_currency"] == "usd"
        assert result["days"] == 7
        assert len(result["tokens"]) == 1
        entry = result["tokens"][0]
        assert entry["symbol"] == "ETH"
        assert entry["id"] == "ethereum"
        assert entry["price_start"] == 2000.0
        assert entry["price_end"] == 2200.0
        assert entry["price_change_pct"] == pytest.approx(10.0)
        assert entry["price_high"] == 2200.0
        assert entry["price_low"] == 2000.0
        assert len(entry["daily_prices"]) == 3

    async def test_api_failure_returns_none_stats(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price_historical._historical_cache", {})
        session = _make_mock_session(429)
        with patch(
            "tools.definitions.get_token_price_historical.aiohttp.ClientSession",
            return_value=session,
        ):
            result = await get_token_price_historical(tokens=["BTC"], days=30)

        entry = result["tokens"][0]
        assert entry["price_start"] is None
        assert entry["price_end"] is None
        assert entry["price_change_pct"] is None
        assert entry["daily_prices"] == []

    async def test_caches_result(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price_historical._historical_cache", {})
        payload = {"prices": [[1_704_067_200_000, 1.0], [1_704_153_600_000, 2.0]]}
        session = _make_mock_session(200, payload)
        with patch(
            "tools.definitions.get_token_price_historical.aiohttp.ClientSession",
            return_value=session,
        ):
            await get_token_price_historical(tokens=["BTC"], days=14)
            await get_token_price_historical(tokens=["BTC"], days=14)

        assert session.get.call_count == 1

    async def test_concurrent_multi_token(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price_historical._historical_cache", {})
        payload = {"prices": [[1_704_067_200_000, 50.0], [1_704_153_600_000, 55.0]]}
        session = _make_mock_session(200, payload)
        with patch(
            "tools.definitions.get_token_price_historical.aiohttp.ClientSession",
            return_value=session,
        ):
            result = await get_token_price_historical(tokens=["BTC", "ETH"], days=30)

        # One market_chart call per token — they cannot be batched.
        assert session.get.call_count == 2
        assert len(result["tokens"]) == 2
        assert {t["symbol"] for t in result["tokens"]} == {"BTC", "ETH"}

    async def test_resolves_unknown_symbol(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price_historical._historical_cache", {})
        coin_map = {"lqty": "liquity", "liquity": "liquity"}
        payload = {"prices": [[1_704_067_200_000, 2.45]]}
        session = _make_mock_session(200, payload)

        async def _mock_load() -> dict[str, str]:
            return coin_map

        with (
            patch(
                "tools.definitions.get_token_price_historical._load_coins_list_index",
                new=_mock_load,
            ),
            patch(
                "tools.definitions.get_token_price_historical.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            result = await get_token_price_historical(tokens=["LQTY"], days=30)

        assert result["tokens"][0]["id"] == "liquity"
        assert result["tokens"][0]["symbol"] == "LQTY"

    async def test_known_symbol_skips_coins_list(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price_historical._historical_cache", {})
        payload = {"prices": [[1_704_067_200_000, 65000.0]]}
        session = _make_mock_session(200, payload)

        load_mock = AsyncMock(return_value={})
        with (
            patch(
                "tools.definitions.get_token_price_historical._load_coins_list_index",
                new=load_mock,
            ),
            patch(
                "tools.definitions.get_token_price_historical.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            await get_token_price_historical(tokens=["BTC"], days=30)

        # All tokens were resolvable via _SYMBOL_TO_ID → coins list never fetched.
        load_mock.assert_not_called()

    async def test_stats_only_omits_daily_prices(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_token_price_historical._historical_cache", {})
        payload = {
            "prices": [
                [1_704_067_200_000, 2000.0],
                [1_704_153_600_000, 2100.0],
                [1_704_240_000_000, 2200.0],
            ]
        }
        session = _make_mock_session(200, payload)
        with patch(
            "tools.definitions.get_token_price_historical.aiohttp.ClientSession",
            return_value=session,
        ):
            result = await get_token_price_historical(tokens=["ETH"], days=7, stats_only=True)

        entry = result["tokens"][0]
        assert entry["daily_prices"] == []
        assert entry["price_start"] == 2000.0
        assert entry["price_end"] == 2200.0
        assert entry["price_change_pct"] == pytest.approx(10.0)
