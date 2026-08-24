# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for tools/definitions/get_dex_volume.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tools.definitions.get_dex_volume import _CACHE_MAX_ENTRIES, _store_cached, get_dex_volume


def _overview() -> dict:
    return {
        "total24h": 1000,
        "protocols": [
            {
                "name": "Uniswap V3",
                "displayName": "Uniswap V3",
                "slug": "uniswap-v3",
                "category": "Dexs",
                "total24h": 600,
                "total7d": 4200,
                "total30d": 18000,
                "change_7dover7d": 10,
                "change_30dover30d": -5,
                "chains": ["Ethereum", "Arbitrum"],
            },
            {
                "name": "Curve DEX",
                "displayName": "Curve DEX",
                "slug": "curve-dex",
                "category": "Dexs",
                "total24h": 400,
                "total7d": 2800,
                "total30d": 12000,
                "change_7dover7d": -2,
                "change_30dover30d": 3,
                "chains": ["Ethereum"],
            },
        ],
    }


def test_dex_volume_cache_is_bounded_and_removes_expired_entries():
    cache = {f"key-{index}": (100.0 + index, index) for index in range(_CACHE_MAX_ENTRIES)}
    cache["expired"] = (1.0, -1)

    _store_cached(cache, "new", 500.0, 999, now=50.0)

    assert "expired" not in cache
    assert cache["new"] == (500.0, 999)
    assert len(cache) == _CACHE_MAX_ENTRIES


@pytest.mark.anyio
async def test_get_dex_volume_filters_and_computes_global_share(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_dex_volume._result_cache", {})
    monkeypatch.setattr("tools.definitions.get_dex_volume._overview_cache", None)
    monkeypatch.setattr(
        "tools.definitions.get_dex_volume._fetch_dex_overview",
        AsyncMock(return_value=(_overview(), None, None)),
    )

    result = await get_dex_volume(protocols=["uniswap-v3"], lookback_days=7, limit=5)

    assert result["total_matching"] == 1
    entry = result["dexes"][0]
    assert entry["protocol"] == "Uniswap V3"
    assert entry["volume_7d_usd"] == pytest.approx(4200)
    assert entry["volume_7d_change_pct"] == pytest.approx(10)
    assert entry["volume_share_pct"] == pytest.approx(60)
    assert "unfiltered" in result["note"]


@pytest.mark.anyio
async def test_get_dex_volume_ranks_by_lookback_and_limits(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_dex_volume._result_cache", {})
    monkeypatch.setattr("tools.definitions.get_dex_volume._overview_cache", None)
    monkeypatch.setattr(
        "tools.definitions.get_dex_volume._fetch_dex_overview",
        AsyncMock(return_value=(_overview(), None, None)),
    )

    result = await get_dex_volume(lookback_days=1, limit=1)

    assert result["total_matching"] == 2
    assert len(result["dexes"]) == 1
    assert result["dexes"][0]["protocol"] == "Uniswap V3"


@pytest.mark.anyio
async def test_get_dex_volume_handles_malformed_protocol_volume(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_dex_volume._result_cache", {})
    monkeypatch.setattr("tools.definitions.get_dex_volume._overview_cache", None)
    monkeypatch.setattr(
        "tools.definitions.get_dex_volume._fetch_dex_overview",
        AsyncMock(
            return_value=(
                {"total24h": 10, "protocols": [{"name": "Broken DEX", "slug": "broken"}]},
                None,
                None,
            )
        ),
    )

    result = await get_dex_volume()

    assert result["dexes"][0]["error_type"] == "malformed_data"
    assert result["dexes"][0]["volume_share_pct"] is None


@pytest.mark.anyio
async def test_get_dex_volume_returns_typed_upstream_error(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_dex_volume._result_cache", {})
    monkeypatch.setattr("tools.definitions.get_dex_volume._overview_cache", None)
    monkeypatch.setattr(
        "tools.definitions.get_dex_volume._fetch_dex_overview",
        AsyncMock(return_value=(None, "timeout", "overview timed out")),
    )

    result = await get_dex_volume()

    assert result["dexes"] == []
    assert result["error_type"] == "timeout"
    assert result["error"] == "overview timed out"


@pytest.mark.anyio
async def test_get_dex_volume_uses_result_cache(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_dex_volume._result_cache", {})
    monkeypatch.setattr("tools.definitions.get_dex_volume._overview_cache", None)
    fetch_overview = AsyncMock(return_value=(_overview(), None, None))
    monkeypatch.setattr("tools.definitions.get_dex_volume._fetch_dex_overview", fetch_overview)

    await get_dex_volume(protocols=["curve-dex"])
    await get_dex_volume(protocols=["curve-dex"])

    fetch_overview.assert_awaited_once()


def test_dex_volume_tool_is_registered():
    from tools.definitions import _ALL_TOOLS
    from tools.definitions.get_dex_volume import TOOL

    assert TOOL in _ALL_TOOLS
