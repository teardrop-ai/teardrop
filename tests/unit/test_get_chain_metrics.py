"""Unit tests for tools/definitions/get_chain_metrics.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tools.definitions.get_chain_metrics import _CACHE_MAX_ENTRIES, _store_cached, get_chain_metrics


def _history(values: list[float]) -> list[dict[str, float | int]]:
    return [{"date": 1_700_000_000 + index * 86_400, "tvl": value} for index, value in enumerate(values)]


def test_chain_metrics_cache_is_bounded_and_removes_expired_entries():
    cache = {f"key-{index}": (100.0 + index, index) for index in range(_CACHE_MAX_ENTRIES)}
    cache["expired"] = (1.0, -1)

    _store_cached(cache, "new", 500.0, 999, now=50.0)

    assert "expired" not in cache
    assert cache["new"] == (500.0, 999)
    assert len(cache) == _CACHE_MAX_ENTRIES


@pytest.mark.anyio
async def test_get_chain_metrics_extracts_tvl_changes_and_fees(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_chain_metrics._result_cache", {})
    monkeypatch.setattr("tools.definitions.get_chain_metrics._chains_cache", None)
    monkeypatch.setattr("tools.definitions.get_chain_metrics._chain_history_cache", {})
    monkeypatch.setattr("tools.definitions.get_chain_metrics._chain_fees_cache", {})
    monkeypatch.setattr(
        "tools.definitions.get_chain_metrics._fetch_chains",
        AsyncMock(
            return_value=(
                [{"name": "Ethereum", "chainId": 1, "tokenSymbol": "ETH", "tvl": 2_000_000}],
                None,
                None,
            )
        ),
    )
    monkeypatch.setattr(
        "tools.definitions.get_chain_metrics._fetch_chain_history",
        AsyncMock(return_value=(_history([100.0] * 30 + [120.0]), None, None)),
    )
    monkeypatch.setattr(
        "tools.definitions.get_chain_metrics._fetch_chain_fees",
        AsyncMock(
            return_value=(
                {
                    "total24h": 1000,
                    "total7d": 7000,
                    "total30d": 30000,
                    "change_7dover7d": 12.5,
                    "change_30dover30d": -4.0,
                },
                None,
                None,
            )
        ),
    )

    result = await get_chain_metrics(chains=["Ethereum"])

    assert result["chains"][0]["chain"] == "Ethereum"
    assert result["chains"][0]["tvl_usd"] == pytest.approx(2_000_000)
    assert result["chains"][0]["tvl_7d_change_pct"] == pytest.approx(20.0)
    assert result["chains"][0]["fees_30d_change_pct"] == pytest.approx(-4.0)


@pytest.mark.anyio
async def test_get_chain_metrics_reports_missing_requested_chain(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_chain_metrics._result_cache", {})
    monkeypatch.setattr("tools.definitions.get_chain_metrics._chains_cache", None)
    monkeypatch.setattr("tools.definitions.get_chain_metrics._chain_history_cache", {})
    monkeypatch.setattr("tools.definitions.get_chain_metrics._chain_fees_cache", {})
    monkeypatch.setattr(
        "tools.definitions.get_chain_metrics._fetch_chains",
        AsyncMock(return_value=([{"name": "Ethereum", "tvl": 1}], None, None)),
    )

    result = await get_chain_metrics(chains=["Unknown Chain"])

    assert result["chains"][0]["error_type"] == "not_found"
    assert result["chains"][0]["tvl_usd"] is None


@pytest.mark.anyio
async def test_get_chain_metrics_fails_open_on_supplement_errors(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_chain_metrics._result_cache", {})
    monkeypatch.setattr("tools.definitions.get_chain_metrics._chains_cache", None)
    monkeypatch.setattr("tools.definitions.get_chain_metrics._chain_history_cache", {})
    monkeypatch.setattr("tools.definitions.get_chain_metrics._chain_fees_cache", {})
    monkeypatch.setattr(
        "tools.definitions.get_chain_metrics._fetch_chains",
        AsyncMock(return_value=([{"name": "Ethereum", "tvl": 1}], None, None)),
    )
    monkeypatch.setattr(
        "tools.definitions.get_chain_metrics._fetch_chain_history",
        AsyncMock(return_value=(None, "timeout", "history timed out")),
    )
    monkeypatch.setattr(
        "tools.definitions.get_chain_metrics._fetch_chain_fees",
        AsyncMock(return_value=(None, "upstream_error", "fees unavailable")),
    )

    result = await get_chain_metrics(chains=["Ethereum"])

    entry = result["chains"][0]
    assert entry["tvl_usd"] == pytest.approx(1.0)
    assert entry["tvl_7d_change_pct"] is None
    assert entry["error_type"] == "partial_data"
    assert "history timed out" in entry["error"]


@pytest.mark.anyio
async def test_get_chain_metrics_uses_result_cache(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_chain_metrics._result_cache", {})
    monkeypatch.setattr("tools.definitions.get_chain_metrics._chains_cache", None)
    monkeypatch.setattr("tools.definitions.get_chain_metrics._chain_history_cache", {})
    monkeypatch.setattr("tools.definitions.get_chain_metrics._chain_fees_cache", {})
    fetch_chains = AsyncMock(return_value=([{"name": "Ethereum", "tvl": 1}], None, None))
    monkeypatch.setattr("tools.definitions.get_chain_metrics._fetch_chains", fetch_chains)
    monkeypatch.setattr(
        "tools.definitions.get_chain_metrics._fetch_chain_history",
        AsyncMock(return_value=([], None, None)),
    )
    monkeypatch.setattr(
        "tools.definitions.get_chain_metrics._fetch_chain_fees",
        AsyncMock(return_value=({}, None, None)),
    )

    await get_chain_metrics(chains=["Ethereum"])
    await get_chain_metrics(chains=["Ethereum"])

    fetch_chains.assert_awaited_once()


def test_chain_metrics_tool_is_registered():
    from tools.definitions import _ALL_TOOLS
    from tools.definitions.get_chain_metrics import TOOL

    assert TOOL in _ALL_TOOLS
