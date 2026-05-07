"""Unit tests for tools/definitions/get_protocol_tvl.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tools.definitions.get_protocol_tvl import get_protocol_tvl


@pytest.mark.anyio
async def test_include_historical_prefers_detail_and_fetches_fallback_in_parallel(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})

    detail_payload = {
        "chainTvls": {
            "Ethereum": {"tvl": [{"date": 1700000000, "totalLiquidityUSD": 1000.0}]},
            "Base": {"tvl": [{"date": 1700000000, "totalLiquidityUSD": 200.0}]},
        },
        "tvl": [
            {"date": 1700000000, "totalLiquidityUSD": 1000.0},
            {"date": 1700086400, "totalLiquidityUSD": 1100.0},
        ],
    }

    detail_mock = AsyncMock(return_value=detail_payload)
    fallback_mock = AsyncMock(return_value=9999.0)

    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_protocol_detail", detail_mock)
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", fallback_mock)

    result = await get_protocol_tvl("aave-v3", include_historical=True, days=30)

    assert detail_mock.await_count == 1
    assert fallback_mock.await_count == 1
    assert result["protocol"] == "aave-v3"
    assert result["current_tvl_usd"] == pytest.approx(1100.0)
    assert len(result["chain_breakdown"]) == 2
    assert result["historical_series"]


@pytest.mark.anyio
async def test_include_historical_uses_current_tvl_fallback_when_detail_unavailable(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})

    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_protocol_detail", AsyncMock(return_value=None))
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", AsyncMock(return_value=1234.56))

    result = await get_protocol_tvl("curve-dex", include_historical=True, days=30)

    assert result["protocol"] == "curve-dex"
    assert result["current_tvl_usd"] == pytest.approx(1234.56)
    assert result["historical_series"] is None
    assert "fallback" in result["note"].lower()


@pytest.mark.anyio
async def test_include_historical_returns_graceful_empty_result_when_all_sources_fail(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})

    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_protocol_detail", AsyncMock(return_value=None))
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", AsyncMock(return_value=None))

    result = await get_protocol_tvl("unknown-protocol", include_historical=True, days=30)

    assert result["current_tvl_usd"] is None
    assert result["historical_series"] is None
    assert result["chain_breakdown"] == []
    assert "unavailable" in result["note"].lower()


@pytest.mark.anyio
async def test_batch_protocols_returns_list_and_deduplicates(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})

    async def _fake_fetch(slug: str):
        return {
            "aave-v3": 1000.0,
            "compound-v3": 500.0,
        }.get(slug)

    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", _fake_fetch)

    result = await get_protocol_tvl(protocols=["aave-v3", "compound-v3", "aave-v3"])

    assert isinstance(result, list)
    assert len(result) == 2
    assert [item["protocol"] for item in result] == ["aave-v3", "compound-v3"]
    assert result[0]["current_tvl_usd"] == pytest.approx(1000.0)
