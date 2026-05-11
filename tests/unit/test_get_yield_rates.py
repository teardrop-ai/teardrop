"""Unit tests for tools/definitions/get_yield_rates.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tools.definitions.get_yield_rates import get_yield_rates


@pytest.mark.anyio
async def test_get_yield_rates_preserves_apy_mean_7d(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_yield_rates._pools_cache", {})
    monkeypatch.setattr(
        "tools.definitions.get_yield_rates._fetch_pools",
        AsyncMock(
            return_value=[
                {
                    "pool": "pool-1",
                    "project": "aave-v3",
                    "symbol": "USDC",
                    "chain": "Ethereum",
                    "tvlUsd": 2_000_000,
                    "apy": 5.2,
                    "apyMean7d": 5.0,
                    "apyMean30d": 4.7,
                    "apyBase": 3.1,
                    "apyReward": 2.1,
                    "stablecoin": True,
                    "ilRisk": "no",
                }
            ]
        ),
    )

    result = await get_yield_rates(protocols=["aave-v3"], min_tvl_usd=0, limit=5)

    assert result["total_matching"] == 1
    assert result["pools"][0]["apy_mean_7d"] == pytest.approx(5.0)
    assert result["pools"][0]["apy_mean_30d"] == pytest.approx(4.7)


@pytest.mark.anyio
async def test_get_yield_rates_handles_malformed_apy_mean_7d(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_yield_rates._pools_cache", {})
    monkeypatch.setattr(
        "tools.definitions.get_yield_rates._fetch_pools",
        AsyncMock(
            return_value=[
                {
                    "pool": "pool-2",
                    "project": "curve",
                    "symbol": "USDT",
                    "chain": "Base",
                    "tvlUsd": 3_000_000,
                    "apy": 4.4,
                    "apyMean7d": "not-a-number",
                    "apyMean30d": 4.0,
                    "apyBase": 3.8,
                    "apyReward": 0.6,
                    "stablecoin": True,
                    "ilRisk": "no",
                }
            ]
        ),
    )

    result = await get_yield_rates(min_tvl_usd=0, limit=5)

    assert result["total_matching"] == 1
    assert result["pools"][0]["apy_mean_7d"] is None


@pytest.mark.anyio
async def test_get_yield_rates_uses_redis_cache_when_available(monkeypatch):
    class _FakeRedis:
        async def get(self, key):
            assert key == "tool:get_yield_rates:pools:all"
            return '[{"pool":"pool-1","project":"aave-v3","symbol":"USDC","chain":"Ethereum","tvlUsd":2000000,"apy":5.2}]'

    monkeypatch.setattr("tools.definitions.get_yield_rates.get_redis", lambda: _FakeRedis())
    monkeypatch.setattr("tools.definitions.get_yield_rates._pools_cache", {})
    mock_fetch = AsyncMock(return_value=[])
    monkeypatch.setattr("tools.definitions.get_yield_rates._fetch_pools", mock_fetch)

    result = await get_yield_rates(min_tvl_usd=0, limit=5)

    assert result["total_matching"] == 1
    assert result["pools"][0]["project"] == "aave-v3"
    mock_fetch.assert_not_awaited()


@pytest.mark.anyio
async def test_get_yield_rates_stable_only_filters_non_stable(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_yield_rates._pools_cache", {})
    monkeypatch.setattr(
        "tools.definitions.get_yield_rates._fetch_pools",
        AsyncMock(
            return_value=[
                {
                    "pool": "pool-stable",
                    "project": "aave-v3",
                    "symbol": "USDC",
                    "chain": "Base",
                    "tvlUsd": 4_000_000,
                    "apy": 5.2,
                    "apyMean7d": 5.0,
                    "apyMean30d": 4.8,
                    "apyBase": 4.5,
                    "apyReward": 0.7,
                    "stablecoin": True,
                    "ilRisk": "no",
                },
                {
                    "pool": "pool-non-stable",
                    "project": "curve",
                    "symbol": "ETH-USDC",
                    "chain": "Base",
                    "tvlUsd": 8_000_000,
                    "apy": 8.9,
                    "apyMean7d": 7.1,
                    "apyMean30d": 4.2,
                    "apyBase": 2.5,
                    "apyReward": 6.4,
                    "stablecoin": False,
                    "ilRisk": "yes",
                },
            ]
        ),
    )

    result = await get_yield_rates(min_tvl_usd=0, stable_only=True, limit=5)

    assert result["total_matching"] == 1
    assert len(result["pools"]) == 1
    assert result["pools"][0]["stable"] is True


@pytest.mark.anyio
async def test_get_yield_rates_stable_only_sorts_by_30d_mean(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_yield_rates._pools_cache", {})
    monkeypatch.setattr(
        "tools.definitions.get_yield_rates._fetch_pools",
        AsyncMock(
            return_value=[
                {
                    "pool": "pool-spot-high",
                    "project": "project-a",
                    "symbol": "USDC",
                    "chain": "Ethereum",
                    "tvlUsd": 9_000_000,
                    "apy": 9.0,
                    "apyMean7d": 7.0,
                    "apyMean30d": 3.0,
                    "apyBase": 2.0,
                    "apyReward": 7.0,
                    "stablecoin": True,
                    "ilRisk": "no",
                },
                {
                    "pool": "pool-spot-lower",
                    "project": "project-b",
                    "symbol": "USDC",
                    "chain": "Ethereum",
                    "tvlUsd": 7_000_000,
                    "apy": 6.0,
                    "apyMean7d": 5.9,
                    "apyMean30d": 5.5,
                    "apyBase": 5.0,
                    "apyReward": 1.0,
                    "stablecoin": True,
                    "ilRisk": "no",
                },
            ]
        ),
    )

    result = await get_yield_rates(min_tvl_usd=0, stable_only=True, limit=5)

    assert result["total_matching"] == 2
    assert result["pools"][0]["pool_id"] == "pool-spot-lower"
    assert result["filters_applied"]["stable_only"] is True
