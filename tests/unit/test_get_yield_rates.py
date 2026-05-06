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
