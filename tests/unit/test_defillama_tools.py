"""Unit tests for tools/definitions/get_protocol_tvl.py and get_yield_rates.py.

All HTTP calls are mocked — no live DeFiLlama network requests during tests.
Coverage: input validation (including injection patterns), happy paths,
error conditions (404, timeout, malformed response), caching behaviour,
and output field correctness.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

import tools.definitions.get_yield_rates as yield_module
from tools.definitions.get_protocol_tvl import (
    GetProtocolTvlInput,
    _compute_change_pct,
    _extract_chain_breakdown,
    _extract_historical_series,
    get_protocol_tvl,
)
from tools.definitions.get_yield_rates import (
    GetYieldRatesInput,
    _pool_to_entry,
    _resolve_apy,
    get_yield_rates,
)

# ─── Shared mock helpers ───────────────────────────────────────────────────────


def _mock_session_text(status: int, text: str = "") -> MagicMock:
    """Build a mock aiohttp.ClientSession whose response returns plain text."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.text = AsyncMock(return_value=text)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _mock_session_json(status: int, payload: dict | list | None = None) -> MagicMock:
    """Build a mock aiohttp.ClientSession whose response returns JSON."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=payload or {})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


# ═══════════════════════════════════════════════════════════════════════════════
# get_protocol_tvl tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetProtocolTvlInput:
    def test_valid_minimal(self):
        inp = GetProtocolTvlInput(protocol="aave-v3")
        assert inp.protocol == "aave-v3"
        assert inp.include_historical is False
        assert inp.days == 30

    def test_slug_normalised_to_lowercase(self):
        inp = GetProtocolTvlInput(protocol="Aave-V3")
        assert inp.protocol == "aave-v3"

    def test_path_traversal_rejected(self):
        with pytest.raises(ValidationError):
            GetProtocolTvlInput(protocol="../../etc/passwd")

    def test_sql_injection_rejected(self):
        with pytest.raises(ValidationError):
            GetProtocolTvlInput(protocol="'; DROP TABLE--")

    def test_shell_injection_rejected(self):
        with pytest.raises(ValidationError):
            GetProtocolTvlInput(protocol="aave; rm -rf /")

    def test_slug_too_long_rejected(self):
        with pytest.raises(ValidationError):
            GetProtocolTvlInput(protocol="a" * 65)

    def test_empty_slug_rejected(self):
        with pytest.raises(ValidationError):
            GetProtocolTvlInput(protocol="")

    def test_days_lower_bound(self):
        inp = GetProtocolTvlInput(protocol="aave", days=1)
        assert inp.days == 1

    def test_days_upper_bound(self):
        inp = GetProtocolTvlInput(protocol="aave", days=365)
        assert inp.days == 365

    def test_days_zero_rejected(self):
        with pytest.raises(ValidationError):
            GetProtocolTvlInput(protocol="aave", days=0)

    def test_days_over_365_rejected(self):
        with pytest.raises(ValidationError):
            GetProtocolTvlInput(protocol="aave", days=366)


class TestExtractChainBreakdown:
    def _make_detail(self, chains: dict) -> dict:
        return {"chainTvls": chains}

    def test_extracts_and_sorts_by_tvl_desc(self):
        detail = self._make_detail(
            {
                "Ethereum": {"tvl": [{"date": 1700000000, "totalLiquidityUSD": 1_000_000}]},
                "Base": {"tvl": [{"date": 1700000000, "totalLiquidityUSD": 5_000_000}]},
            }
        )
        entries = _extract_chain_breakdown(detail)
        assert entries[0].chain == "Base"
        assert entries[0].tvl_usd == 5_000_000
        assert entries[1].chain == "Ethereum"

    def test_caps_at_10_entries(self):
        chains = {f"Chain{i}": {"tvl": [{"date": 1700000000, "totalLiquidityUSD": float(i * 100)}]} for i in range(1, 20)}
        entries = _extract_chain_breakdown({"chainTvls": chains})
        assert len(entries) == 10

    def test_zero_tvl_chains_excluded(self):
        detail = self._make_detail({"Ethereum": {"tvl": [{"date": 1700000000, "totalLiquidityUSD": 0}]}})
        entries = _extract_chain_breakdown(detail)
        assert entries == []

    def test_malformed_chain_data_skipped(self):
        detail = self._make_detail({"BadChain": "not-a-dict"})
        entries = _extract_chain_breakdown(detail)
        assert entries == []

    def test_empty_chaintvls(self):
        assert _extract_chain_breakdown({"chainTvls": {}}) == []

    def test_missing_chaintvls_key(self):
        assert _extract_chain_breakdown({}) == []


class TestExtractHistoricalSeries:
    def _base_tvl(self) -> list[dict]:
        # 40 daily entries starting at unix timestamp for 2024-01-01
        base = 1_704_067_200
        return [{"date": base + i * 86400, "totalLiquidityUSD": float(100_000 + i * 1_000)} for i in range(40)]

    def test_returns_daily_points(self):
        detail = {"tvl": self._base_tvl()}
        series = _extract_historical_series(detail, days=30)
        assert len(series) > 0
        assert all(hasattr(p, "date") and hasattr(p, "tvl_usd") for p in series)

    def test_respects_days_window(self):
        detail = {"tvl": self._base_tvl()}
        series_7 = _extract_historical_series(detail, days=7)
        series_30 = _extract_historical_series(detail, days=30)
        assert len(series_7) <= len(series_30)

    def test_capped_at_90_points(self):
        base = 1_704_067_200
        long_tvl = [{"date": base + i * 86400, "totalLiquidityUSD": float(i)} for i in range(200)]
        series = _extract_historical_series({"tvl": long_tvl}, days=365)
        assert len(series) <= 90

    def test_iso_date_format(self):
        detail = {"tvl": [{"date": 1_704_067_200, "totalLiquidityUSD": 42_000_000.0}]}
        series = _extract_historical_series(detail, days=365)
        assert series[0].date == "2024-01-01"

    def test_malformed_entries_skipped(self):
        detail = {"tvl": [{"date": "not-a-ts", "totalLiquidityUSD": 1_000}, {"date": 1_704_067_200, "totalLiquidityUSD": 9_000}]}
        series = _extract_historical_series(detail, days=365)
        assert len(series) == 1
        assert series[0].tvl_usd == 9_000

    def test_empty_tvl_array(self):
        assert _extract_historical_series({"tvl": []}, days=30) == []

    def test_missing_tvl_key(self):
        assert _extract_historical_series({}, days=30) == []


class TestComputeChangePct:
    def test_positive_change(self):
        series = [("2024-01-01", 100.0), ("2024-01-02", 110.0), ("2024-01-03", 120.0)]
        # 7d: looking back past beginning → uses index 0
        result = _compute_change_pct(series, 7)
        assert result == pytest.approx(20.0)

    def test_negative_change(self):
        series = [("2024-01-01", 200.0), ("2024-01-08", 100.0)]
        result = _compute_change_pct(series, 7)
        assert result == pytest.approx(-50.0)

    def test_zero_base_returns_none(self):
        series = [("2024-01-01", 0.0), ("2024-01-08", 100.0)]
        assert _compute_change_pct(series, 7) is None

    def test_empty_series_returns_none(self):
        assert _compute_change_pct([], 7) is None

    def test_single_entry_returns_none(self):
        assert _compute_change_pct([("2024-01-01", 100.0)], 7) is None


class TestGetProtocolTvl:
    async def test_current_tvl_fast_path(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
        session = _mock_session_text(200, "42000000000.5")
        with patch("tools.definitions.get_protocol_tvl.aiohttp.ClientSession", return_value=session):
            result = await get_protocol_tvl("aave")
        assert result["current_tvl_usd"] == pytest.approx(42_000_000_000.5)
        assert result["protocol"] == "aave"
        assert result["chain_breakdown"] == []
        assert result["historical_series"] is None

    async def test_protocol_not_found_returns_graceful_error(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
        session = _mock_session_text(404, "")
        with patch("tools.definitions.get_protocol_tvl.aiohttp.ClientSession", return_value=session):
            result = await get_protocol_tvl("nonexistent-protocol-xyz")
        assert result["current_tvl_usd"] is None
        assert "not found" in result["note"].lower()

    async def test_network_error_returns_graceful_result(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})

        def _raise(*_a, **_kw):
            raise OSError("network unreachable")

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=_raise)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.definitions.get_protocol_tvl.aiohttp.ClientSession", return_value=mock_session):
            result = await get_protocol_tvl("aave")
        assert result["current_tvl_usd"] is None

    async def test_historical_path_returns_series_and_chain_breakdown(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
        base_ts = 1_704_067_200
        payload = {
            "chainTvls": {
                "Ethereum": {"tvl": [{"date": base_ts, "totalLiquidityUSD": 5_000_000_000}]},
                "Base": {"tvl": [{"date": base_ts, "totalLiquidityUSD": 1_000_000_000}]},
            },
            "tvl": [{"date": base_ts + i * 86400, "totalLiquidityUSD": float(6_000_000_000 + i * 1_000_000)} for i in range(35)],
        }
        session = _mock_session_json(200, payload)
        with patch("tools.definitions.get_protocol_tvl.aiohttp.ClientSession", return_value=session):
            result = await get_protocol_tvl("aave", include_historical=True, days=30)
        assert result["historical_series"] is not None
        assert len(result["historical_series"]) <= 30
        assert result["chain_breakdown"][0]["chain"] == "Ethereum"
        assert result["tvl_7d_change_pct"] is not None

    async def test_cache_hit_skips_http(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
        call_count = 0

        async def _counting_fetch(slug: str) -> float | None:
            nonlocal call_count
            call_count += 1
            return 1_000_000.0

        monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", _counting_fetch)
        await get_protocol_tvl("aave")
        await get_protocol_tvl("aave")
        assert call_count == 1  # second call served from cache

    async def test_non_numeric_tvl_response_handled(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
        session = _mock_session_text(200, "not-a-number")
        with patch("tools.definitions.get_protocol_tvl.aiohttp.ClientSession", return_value=session):
            result = await get_protocol_tvl("aave")
        assert result["current_tvl_usd"] is None

    async def test_not_found_result_is_cached(self, test_settings, monkeypatch):
        """404/error results must be cached with a short TTL to avoid re-hitting the network."""
        monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
        call_count = 0

        async def _counting_fetch(slug: str) -> float | None:
            nonlocal call_count
            call_count += 1
            return None  # simulate 404 / network failure

        monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", _counting_fetch)
        result1 = await get_protocol_tvl("nonexistent-slug")
        result2 = await get_protocol_tvl("nonexistent-slug")
        assert call_count == 1  # second call served from cache
        assert result1["current_tvl_usd"] is None
        assert result2["current_tvl_usd"] is None

    async def test_change_pct_uses_full_series_not_trimmed_window(self, test_settings, monkeypatch):
        """tvl_30d_change_pct must be computed from the full available data, not the
        window-trimmed historical_series. With days=7, tvl_30d_change_pct should
        still reflect a genuine 30-day window when 30+ days of data exist."""
        monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
        base_ts = 1_704_067_200  # 2024-01-01
        # 60 daily entries; TVL grows linearly by $10k/day from $1M.
        # current (day 59) = $1,590,000; 30 days ago (day 29) = $1,290,000 → ~23.3% 30d change.
        # Using the 7-entry trimmed series would give only ~4% (same window as 7d).
        tvl_entries = [{"date": base_ts + i * 86400, "totalLiquidityUSD": float(1_000_000 + i * 10_000)} for i in range(60)]
        payload = {"chainTvls": {}, "tvl": tvl_entries}
        session = _mock_session_json(200, payload)
        with patch("tools.definitions.get_protocol_tvl.aiohttp.ClientSession", return_value=session):
            result = await get_protocol_tvl("aave", include_historical=True, days=7)
        # historical_series is trimmed to ≤7 entries
        assert len(result["historical_series"]) <= 7
        # 30d change must reflect the genuine ~23% growth from full series, not ~4% from trimmed
        assert result["tvl_30d_change_pct"] is not None
        assert result["tvl_30d_change_pct"] > 20.0

    async def test_historical_detail_failure_falls_back_to_current_tvl(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
        detail_fetch = AsyncMock(return_value=None)
        current_fetch = AsyncMock(return_value=123_456_789.0)
        monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_protocol_detail", detail_fetch)
        monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", current_fetch)

        result = await get_protocol_tvl("aave-v3", include_historical=True)

        assert result["current_tvl_usd"] == pytest.approx(123_456_789.0)
        assert result["historical_series"] is None
        assert result["chain_breakdown"] == []
        assert "fallback" in result["note"].lower()
        detail_fetch.assert_awaited_once()
        current_fetch.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# get_yield_rates tests
# ═══════════════════════════════════════════════════════════════════════════════


_SAMPLE_POOLS = [
    {
        "pool": "pool-001",
        "project": "aave-v3",
        "symbol": "USDC",
        "chain": "Ethereum",
        "tvlUsd": 500_000_000,
        "apy": 4.5,
        "apyBase": 3.0,
        "apyReward": 1.5,
        "stablecoin": True,
        "ilRisk": "no",
    },
    {
        "pool": "pool-002",
        "project": "compound-v3",
        "symbol": "USDC",
        "chain": "Ethereum",
        "tvlUsd": 200_000_000,
        "apy": 3.8,
        "apyBase": 3.8,
        "apyReward": None,
        "stablecoin": True,
        "ilRisk": "no",
    },
    {
        "pool": "pool-003",
        "project": "curve-dex",
        "symbol": "3pool",
        "chain": "Ethereum",
        "tvlUsd": 800_000_000,
        "apy": 2.1,
        "apyBase": 2.1,
        "apyReward": None,
        "stablecoin": True,
        "ilRisk": "low",
    },
    {
        "pool": "pool-004",
        "project": "aave-v3",
        "symbol": "USDC",
        "chain": "Base",
        "tvlUsd": 50_000_000,
        "apy": 5.2,
        "apyBase": 4.0,
        "apyReward": 1.2,
        "stablecoin": True,
        "ilRisk": "no",
    },
    {
        "pool": "pool-005",
        "project": "yearn-v3",
        "symbol": "ETH-USDC",
        "chain": "Ethereum",
        "tvlUsd": 100_000,  # below default 1M min_tvl
        "apy": 99.9,
        "apyBase": 99.9,
        "apyReward": None,
        "stablecoin": False,
        "ilRisk": "high",
    },
]


class TestGetYieldRatesInput:
    def test_valid_minimal(self):
        inp = GetYieldRatesInput()
        assert inp.protocols is None
        assert inp.chain is None
        assert inp.min_tvl_usd == 1_000_000.0
        assert inp.limit == 20

    def test_protocol_slug_injection_rejected(self):
        with pytest.raises(ValidationError):
            GetYieldRatesInput(protocols=["../../etc/passwd"])

    def test_protocol_sql_injection_rejected(self):
        with pytest.raises(ValidationError):
            GetYieldRatesInput(protocols=["'; DROP TABLE--"])

    def test_chain_injection_rejected(self):
        with pytest.raises(ValidationError):
            GetYieldRatesInput(chain="'; DROP TABLE--")

    def test_limit_above_50_rejected(self):
        with pytest.raises(ValidationError):
            GetYieldRatesInput(limit=51)

    def test_limit_zero_rejected(self):
        with pytest.raises(ValidationError):
            GetYieldRatesInput(limit=0)

    def test_min_tvl_negative_rejected(self):
        with pytest.raises(ValidationError):
            GetYieldRatesInput(min_tvl_usd=-1.0)

    def test_min_apy_negative_rejected(self):
        with pytest.raises(ValidationError):
            GetYieldRatesInput(min_apy=-0.1)

    def test_valid_with_all_filters(self):
        inp = GetYieldRatesInput(
            protocols=["aave-v3", "compound-v3"],
            chain="Ethereum",
            min_tvl_usd=1_000_000,
            min_apy=1.0,
            limit=10,
        )
        assert inp.protocols == ["aave-v3", "compound-v3"]

    def test_empty_protocols_list_accepted_and_means_no_filter(self):
        """Empty list must be accepted by the validator and treated the same as None."""
        inp = GetYieldRatesInput(protocols=[])
        assert inp.protocols == []

    def test_symbols_any_accepted(self):
        inp = GetYieldRatesInput(symbols_any=["USDC", "ETH"])
        assert inp.symbols_any == ["USDC", "ETH"]


class TestResolveApy:
    def test_uses_spot_apy_when_available(self):
        pool = {"apy": 5.0, "apyMean30d": 3.0}
        assert _resolve_apy(pool) == 5.0

    def test_falls_back_to_mean_when_spot_null(self):
        pool = {"apy": None, "apyMean30d": 3.5}
        assert _resolve_apy(pool) == 3.5

    def test_returns_zero_when_both_null(self):
        pool = {"apy": None, "apyMean30d": None}
        assert _resolve_apy(pool) == 0.0

    def test_handles_string_apy(self):
        # Invalid string APY → float() fails → falls back to apyMean30d.
        pool = {"apy": "not-a-number", "apyMean30d": 2.0}
        assert _resolve_apy(pool) == 2.0


class TestPoolToEntry:
    def test_maps_fields_correctly(self):
        entry = _pool_to_entry(_SAMPLE_POOLS[0])
        assert entry.pool_id == "pool-001"
        assert entry.project == "aave-v3"
        assert entry.symbol == "USDC"
        assert entry.chain == "Ethereum"
        assert entry.tvl_usd == 500_000_000
        assert entry.apy == 4.5
        assert entry.apy_base == 3.0
        assert entry.apy_reward == 1.5
        assert entry.stable is True
        assert entry.il_risk == "no"

    def test_null_reward_apy_mapped_to_none(self):
        entry = _pool_to_entry(_SAMPLE_POOLS[1])
        assert entry.apy_reward is None

    def test_null_il_risk_mapped_to_none(self):
        pool = {**_SAMPLE_POOLS[0], "ilRisk": None}
        entry = _pool_to_entry(pool)
        assert entry.il_risk is None

    def test_non_numeric_apy_base_mapped_to_none(self):
        """Non-numeric apyBase must not raise — must map to None instead of crashing the tool."""
        pool = {**_SAMPLE_POOLS[0], "apyBase": "N/A"}
        entry = _pool_to_entry(pool)
        assert entry.apy_base is None

    def test_non_numeric_apy_reward_mapped_to_none(self):
        """Non-numeric apyReward must not raise — must map to None instead of crashing the tool."""
        pool = {**_SAMPLE_POOLS[0], "apyReward": "N/A"}
        entry = _pool_to_entry(pool)
        assert entry.apy_reward is None


class TestGetYieldRates:
    async def _call_with_pools(self, monkeypatch, pools: list, **kwargs) -> dict:
        """Helper: patch cache + fetch, then call get_yield_rates."""
        monkeypatch.setattr("tools.definitions.get_yield_rates._pools_cache", {})
        monkeypatch.setattr("tools.definitions.get_yield_rates._fetch_pools", AsyncMock(return_value=pools))
        return await get_yield_rates(**kwargs)

    async def test_returns_pools_sorted_by_apy_desc(self, test_settings, monkeypatch):
        result = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS)
        apys = [p["apy"] for p in result["pools"]]
        assert apys == sorted(apys, reverse=True)

    async def test_protocols_filter(self, test_settings, monkeypatch):
        result = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS, protocols=["aave-v3"])
        assert all(p["project"] == "aave-v3" for p in result["pools"])

    async def test_chain_filter_case_insensitive(self, test_settings, monkeypatch):
        result = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS, chain="base")
        assert all(p["chain"].lower() == "base" for p in result["pools"])

    async def test_min_tvl_filter_excludes_low_tvl(self, test_settings, monkeypatch):
        # pool-005 has tvlUsd=100_000, below 1M default
        result = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS)
        pool_ids = [p["pool_id"] for p in result["pools"]]
        assert "pool-005" not in pool_ids

    async def test_min_apy_filter(self, test_settings, monkeypatch):
        result = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS, min_apy=4.0)
        assert all(p["apy"] >= 4.0 for p in result["pools"])

    async def test_limit_respected(self, test_settings, monkeypatch):
        result = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS, limit=2)
        assert len(result["pools"]) <= 2

    async def test_total_matching_reflects_pre_limit_count(self, test_settings, monkeypatch):
        # All 4 pools above 1M TVL; limit=2 → total_matching should be 4
        result = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS, limit=2)
        assert result["total_matching"] == 4

    async def test_filters_applied_echoed_in_output(self, test_settings, monkeypatch):
        result = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS, protocols=["aave-v3"], chain="Ethereum")
        assert result["filters_applied"]["protocols"] == ["aave-v3"]
        assert result["filters_applied"]["chain"] == "Ethereum"

    async def test_empty_pool_list_returns_gracefully(self, test_settings, monkeypatch):
        result = await self._call_with_pools(monkeypatch, [])
        assert result["pools"] == []
        assert result["total_matching"] == 0

    async def test_network_error_returns_empty_result(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_yield_rates._pools_cache", {})

        async def _fail() -> list:
            raise OSError("network unreachable")

        monkeypatch.setattr("tools.definitions.get_yield_rates._fetch_pools", _fail)
        result = await get_yield_rates()
        assert result["pools"] == []
        assert "unavailable" in result["note"].lower()

    async def test_cache_prevents_second_fetch(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_yield_rates._pools_cache", {})
        fetch_mock = AsyncMock(return_value=_SAMPLE_POOLS)
        monkeypatch.setattr("tools.definitions.get_yield_rates._fetch_pools", fetch_mock)
        await get_yield_rates()
        await get_yield_rates()
        fetch_mock.assert_called_once()

    async def test_null_apy_pool_uses_fallback(self, test_settings, monkeypatch):
        pool_with_null_apy = {
            **_SAMPLE_POOLS[0],
            "pool": "null-apy-pool",
            "apy": None,
            "apyMean30d": 7.0,
        }
        result = await self._call_with_pools(monkeypatch, [pool_with_null_apy])
        assert result["pools"][0]["apy"] == 7.0

    async def test_defillama_500_returns_empty(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_yield_rates._pools_cache", {})
        session = _mock_session_json(500, {})
        with patch("tools.definitions.get_yield_rates.aiohttp.ClientSession", return_value=session):
            result = await get_yield_rates()
        assert result["pools"] == []

    async def test_empty_protocols_list_returns_all_protocols(self, test_settings, monkeypatch):
        """protocols=[] must behave identically to protocols=None — no protocol filter applied."""
        result = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS, protocols=[])
        # 4 of the 5 sample pools are above the default 1M TVL threshold
        assert result["total_matching"] == 4

    async def test_symbols_any_filters_to_matching_symbols(self, test_settings, monkeypatch):
        result = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS, symbols_any=["USDC"])
        assert result["total_matching"] == 3
        assert all("usdc" in p["symbol"].lower() for p in result["pools"])

    async def test_symbols_any_case_insensitive(self, test_settings, monkeypatch):
        result = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS, symbols_any=["eth"])
        assert result["total_matching"] == 0
        result2 = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS, min_tvl_usd=0, symbols_any=["eth"])
        assert result2["total_matching"] == 1
        assert result2["pools"][0]["symbol"] == "ETH-USDC"

    async def test_symbols_any_empty_behaves_as_no_filter(self, test_settings, monkeypatch):
        result = await self._call_with_pools(monkeypatch, _SAMPLE_POOLS, symbols_any=[])
        assert result["total_matching"] == 4

    async def test_error_fetch_uses_short_cache_ttl(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_yield_rates._pools_cache", {})
        monkeypatch.setattr("tools.definitions.get_yield_rates.time.monotonic", lambda: 100.0)
        monkeypatch.setattr("tools.definitions.get_yield_rates._fetch_pools", AsyncMock(return_value=[]))

        await get_yield_rates()
        expires_at, _ = yield_module._pools_cache[yield_module._POOLS_CACHE_KEY]
        assert expires_at == pytest.approx(160.0)

    async def test_success_fetch_uses_standard_cache_ttl(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_yield_rates._pools_cache", {})
        monkeypatch.setattr("tools.definitions.get_yield_rates.time.monotonic", lambda: 100.0)
        monkeypatch.setattr("tools.definitions.get_yield_rates._fetch_pools", AsyncMock(return_value=_SAMPLE_POOLS))

        await get_yield_rates()
        expires_at, _ = yield_module._pools_cache[yield_module._POOLS_CACHE_KEY]
        assert expires_at == pytest.approx(400.0)


# ─── Tool registration sanity check ───────────────────────────────────────────


class TestToolRegistration:
    def test_get_protocol_tvl_registered(self):
        from tools import registry

        tool = registry.get("get_protocol_tvl")
        assert tool is not None
        assert tool.name == "get_protocol_tvl"

    def test_get_yield_rates_registered(self):
        from tools import registry

        tool = registry.get("get_yield_rates")
        assert tool is not None
        assert tool.name == "get_yield_rates"

    def test_both_tools_in_list_latest(self):
        from tools import registry

        names = {t.name for t in registry.list_latest()}
        assert "get_protocol_tvl" in names
        assert "get_yield_rates" in names
