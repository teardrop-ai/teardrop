# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for tools/definitions/get_protocol_tvl.py."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from tools.definitions.get_protocol_tvl import TOOL, get_protocol_tvl


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
    assert result["provenance"]["provider"] == "DeFiLlama"
    assert result["provenance"]["cache_hit"] is False
    assert result["provenance"]["source_fetched_at"] is not None
    assert result["provenance"]["source_urls"] == [
        "https://api.llama.fi/protocol/aave-v3",
        "https://api.llama.fi/tvl/aave-v3",
        "https://api.llama.fi/summary/fees/aave-v3",
        "https://api.llama.fi/summary/fees/aave-v3?dataType=dailyRevenue",
    ]


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
    assert result["provenance"]["source_fetched_at"] is not None


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
    assert result["provenance"]["source_fetched_at"] is None


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


@pytest.mark.anyio
async def test_slug_alias_applied_for_spark_protocol(monkeypatch, caplog):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
    current_mock = AsyncMock(return_value=(321.0, None, None))
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", current_mock)

    caplog.set_level("INFO")
    result = await get_protocol_tvl(protocol="spark-protocol")

    current_mock.assert_awaited_once_with("spark")
    assert result["protocol"] == "spark"
    assert "slug alias applied" in caplog.text.lower()


@pytest.mark.anyio
async def test_result_includes_error_fields_on_not_found(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
    monkeypatch.setattr(
        "tools.definitions.get_protocol_tvl._fetch_current_tvl",
        AsyncMock(return_value=(None, "not_found", "Protocol not found on DeFiLlama")),
    )

    result = await get_protocol_tvl(protocol="missing-protocol")

    assert result["current_tvl_usd"] is None
    assert result["error_type"] == "not_found"
    assert "not found" in (result["error"] or "").lower()
    assert result["provenance"]["source_fetched_at"] is None


@pytest.mark.anyio
async def test_protocol_tvl_result_cache_refreshes_retrieval_metadata(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
    current_mock = AsyncMock(return_value=(321.0, None, None))
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", current_mock)

    first = await get_protocol_tvl(protocol="aave-v3")
    second = await get_protocol_tvl(protocol="aave-v3")

    assert second["current_tvl_usd"] == first["current_tvl_usd"]
    assert second["provenance"]["cache_hit"] is True
    assert second["provenance"]["source_fetched_at"] == first["provenance"]["source_fetched_at"]
    assert first["provenance"]["cache_hit"] is False
    assert second is not first
    assert second["provenance"]["cache_age_seconds"] >= 0
    assert second["provenance"]["cache_ttl_seconds"] == 300
    current_mock.assert_awaited_once_with("aave-v3")


def test_protocol_tvl_version_and_schema_include_provenance():
    assert TOOL.version == "1.4.0"
    assert "provenance" in TOOL.output_schema["anyOf"][0]["properties"]


def test_protocol_tvl_output_schema_validates_single_and_batch_results():
    """Regression: $defs must be hoisted to the schema root so $refs resolve."""
    from jsonschema import Draft7Validator

    from tools.definitions.get_protocol_tvl import GetProtocolTvlOutput

    single = GetProtocolTvlOutput(
        protocol="aave-v3",
        current_tvl_usd=1000.0,
        tvl_7d_change_pct=None,
        tvl_30d_change_pct=None,
        chain_breakdown=[],
        historical_series=None,
        note="ok",
    ).model_dump()
    single["provenance"] = {
        "provider": "DeFiLlama",
        "source_urls": ["https://api.llama.fi/tvl/aave-v3"],
        "retrieved_at": "2026-08-16T00:00:00Z",
        "source_fetched_at": None,
        "cache_hit": False,
        "cache_age_seconds": None,
        "cache_ttl_seconds": 300,
    }

    Draft7Validator(TOOL.output_schema).validate(single)
    Draft7Validator(TOOL.output_schema).validate([single])


@pytest.mark.anyio
async def test_batch_continues_after_one_slug_exception(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})

    async def _fake_single(protocol: str | None = None, include_historical: bool = False, days: int = 30):
        if protocol == "compound-v3":
            raise RuntimeError("upstream boom")
        return {
            "protocol": protocol,
            "current_tvl_usd": 1000.0,
            "tvl_7d_change_pct": None,
            "tvl_30d_change_pct": None,
            "chain_breakdown": [],
            "historical_series": None,
            "note": "ok",
            "error": None,
            "error_type": None,
        }

    monkeypatch.setattr("tools.definitions.get_protocol_tvl._get_protocol_tvl_single", _fake_single)

    result = await get_protocol_tvl(protocols=["aave-v3", "compound-v3"])

    assert result[0]["protocol"] == "aave-v3"
    assert result[0]["error_type"] is None
    assert result[1]["protocol"] == "compound-v3"
    assert result[1]["error_type"] == "upstream_error"


@pytest.mark.anyio
async def test_include_historical_extracts_fees_and_revenue(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})

    detail_payload = {
        "tvl": [
            {"date": 1700000000, "totalLiquidityUSD": 1000.0},
            {"date": 1700086400, "totalLiquidityUSD": 1100.0},
        ],
        "fees": [
            {"date": 1700000000, "fees": 100.0},
            {"date": 1700086400, "fees": 150.0},
        ],
        "revenue": [
            {"date": 1700000000, "revenue": 50.0},
            {"date": 1700086400, "revenue": 75.0},
        ],
    }

    monkeypatch.setattr(
        "tools.definitions.get_protocol_tvl._fetch_protocol_detail",
        AsyncMock(return_value=detail_payload),
    )
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", AsyncMock(return_value=9999.0))

    result = await get_protocol_tvl("liquity", include_historical=True, days=30)

    assert result["current_fees_usd"] == pytest.approx(150.0)
    assert result["fees_7d_change_pct"] == pytest.approx(50.0)
    assert result["current_revenue_usd"] == pytest.approx(75.0)
    assert result["revenue_7d_change_pct"] == pytest.approx(50.0)


@pytest.mark.anyio
async def test_include_historical_uses_rolling_value_for_incomplete_current_fee_day(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})

    today = datetime.now(timezone.utc).date()
    start = datetime.combine(today - timedelta(days=30), datetime.min.time(), tzinfo=timezone.utc)
    chart = [[int((start + timedelta(days=offset)).timestamp()), 100.0] for offset in range(31)]
    chart[-8][1] = 400.0
    chart[-1][1] = 0.0
    detail_payload = {
        "tvl": [
            {"date": int(start.timestamp()), "totalLiquidityUSD": 1000.0},
            {"date": int((start + timedelta(days=30)).timestamp()), "totalLiquidityUSD": 1100.0},
        ],
    }

    async def _summary(_slug: str, metric: str, data_type: str | None = None):
        if metric == "fees" and data_type is None:
            return {"total24h": 300.0, "totalDataChart": chart}, None, None
        return None, "not_found", "revenue unavailable"

    monkeypatch.setattr(
        "tools.definitions.get_protocol_tvl._fetch_protocol_detail",
        AsyncMock(return_value=detail_payload),
    )
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", AsyncMock(return_value=9999.0))
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_protocol_summary", _summary)

    result = await get_protocol_tvl("liquity", include_historical=True, days=30)

    assert result["current_fees_usd"] == pytest.approx(300.0)
    assert result["fees_7d_change_pct"] == pytest.approx(-25.0)
    assert result["fees_30d_change_pct"] == pytest.approx(200.0)
    assert result["revenue_error_type"] == "not_found"


@pytest.mark.anyio
async def test_include_historical_fees_fail_open_when_absent(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})

    detail_payload = {
        "tvl": [
            {"date": 1700000000, "totalLiquidityUSD": 1000.0},
            {"date": 1700086400, "totalLiquidityUSD": 1100.0},
        ],
    }

    monkeypatch.setattr(
        "tools.definitions.get_protocol_tvl._fetch_protocol_detail",
        AsyncMock(return_value=detail_payload),
    )
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", AsyncMock(return_value=9999.0))
    monkeypatch.setattr(
        "tools.definitions.get_protocol_tvl._fetch_protocol_summary",
        AsyncMock(return_value=(None, "not_found", "missing")),
    )

    result = await get_protocol_tvl("railgun", include_historical=True, days=30)

    assert result["current_fees_usd"] is None
    assert result["fees_7d_change_pct"] is None
    assert result["current_revenue_usd"] is None
    assert result["revenue_30d_change_pct"] is None


@pytest.mark.anyio
async def test_include_historical_uses_protocol_summary_series(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})

    detail_payload = {
        "tvl": [
            {"date": 1700000000, "totalLiquidityUSD": 1000.0},
            {"date": 1700086400, "totalLiquidityUSD": 1100.0},
        ],
    }

    async def _summary(_slug: str, metric: str, data_type: str | None = None):
        if metric == "fees" and data_type is None:
            return (
                {
                    "total24h": 150.0,
                    "totalDataChart": [[1700000000, 100.0], [1700086400, 150.0]],
                },
                None,
                None,
            )
        return None, "upstream_error", "revenue unavailable"

    monkeypatch.setattr(
        "tools.definitions.get_protocol_tvl._fetch_protocol_detail",
        AsyncMock(return_value=detail_payload),
    )
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", AsyncMock(return_value=9999.0))
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_protocol_summary", _summary)

    result = await get_protocol_tvl("aave-v3", include_historical=True, days=30)

    assert result["current_fees_usd"] == pytest.approx(150.0)
    assert result["fees_7d_change_pct"] == pytest.approx(50.0)
    assert result["current_revenue_usd"] is None
    assert result["revenue_error_type"] == "upstream_error"


@pytest.mark.anyio
async def test_include_historical_fetches_revenue_via_fees_endpoint_with_daily_revenue(monkeypatch):
    """Revenue must be requested through /summary/fees with dataType=dailyRevenue.

    DeFiLlama has no /summary/revenue endpoint (it returns HTTP 500), so revenue
    is served by the fees endpoint with the dailyRevenue dataType. This test
    asserts the fetch is routed correctly and revenue values are extracted.
    """
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})

    detail_payload = {
        "tvl": [
            {"date": 1700000000, "totalLiquidityUSD": 1000.0},
            {"date": 1700086400, "totalLiquidityUSD": 1100.0},
        ],
    }

    calls: list[tuple[str, str, str | None]] = []

    async def _summary(slug: str, metric: str, data_type: str | None = None):
        calls.append((slug, metric, data_type))
        if metric == "fees" and data_type is None:
            return (
                {"total24h": 150.0, "totalDataChart": [[1700000000, 100.0], [1700086400, 150.0]]},
                None,
                None,
            )
        if metric == "fees" and data_type == "dailyRevenue":
            return (
                {"total24h": 75.0, "totalDataChart": [[1700000000, 50.0], [1700086400, 75.0]]},
                None,
                None,
            )
        return None, "upstream_error", "unexpected"

    monkeypatch.setattr(
        "tools.definitions.get_protocol_tvl._fetch_protocol_detail",
        AsyncMock(return_value=detail_payload),
    )
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_current_tvl", AsyncMock(return_value=9999.0))
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._fetch_protocol_summary", _summary)

    result = await get_protocol_tvl("liquity", include_historical=True, days=30)

    assert ("liquity", "fees", None) in calls
    assert ("liquity", "fees", "dailyRevenue") in calls
    assert result["current_fees_usd"] == pytest.approx(150.0)
    assert result["fees_7d_change_pct"] == pytest.approx(50.0)
    assert result["current_revenue_usd"] == pytest.approx(75.0)
    assert result["revenue_7d_change_pct"] == pytest.approx(50.0)
    assert result["revenue_error_type"] is None


@pytest.mark.anyio
async def test_batch_protocols_compact_historical_payload(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})

    async def _fake_single(protocol: str | None = None, include_historical: bool = False, days: int = 30):
        return {
            "protocol": protocol,
            "current_tvl_usd": 1000.0,
            "tvl_7d_change_pct": 1.0,
            "tvl_30d_change_pct": 2.0,
            "chain_breakdown": [{"chain": "Ethereum", "tvl_usd": 1000.0}],
            "historical_series": [{"date": "2026-08-16", "tvl_usd": 1000.0}],
            "current_fees_usd": 10.0,
            "fees_7d_change_pct": 1.0,
            "fees_30d_change_pct": 2.0,
            "current_revenue_usd": None,
            "revenue_7d_change_pct": None,
            "revenue_30d_change_pct": None,
            "revenue_error_type": "upstream_error",
            "note": "ok",
            "error": None,
            "error_type": None,
            "provenance": None,
        }

    monkeypatch.setattr("tools.definitions.get_protocol_tvl._get_protocol_tvl_single", _fake_single)

    result = await get_protocol_tvl(
        protocols=["liquity", "railgun", "tornado-cash"],
        include_historical=True,
        days=30,
    )

    assert len(result) == 3
    assert all(item["chain_breakdown"] == [] for item in result)
    assert all(item["historical_series"] is None for item in result)
    assert all(item["current_fees_usd"] == 10.0 for item in result)
    assert len(json.dumps(result)) < 4000


@pytest.mark.anyio
async def test_batch_timeout_returns_partial_results(monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._BATCH_TIMEOUT_SECONDS", 0.03)

    async def _fake_single(protocol: str | None = None, include_historical: bool = False, days: int = 30):
        if protocol == "slow-protocol":
            await asyncio.sleep(0.1)
        return {
            "protocol": protocol,
            "current_tvl_usd": 1000.0,
            "tvl_7d_change_pct": None,
            "tvl_30d_change_pct": None,
            "chain_breakdown": [],
            "historical_series": None,
            "note": "ok",
            "error": None,
            "error_type": None,
        }

    monkeypatch.setattr("tools.definitions.get_protocol_tvl._get_protocol_tvl_single", _fake_single)

    result = await get_protocol_tvl(protocols=["slow-protocol", "aave-v3"])

    assert result[0]["protocol"] == "slow-protocol"
    assert result[0]["error_type"] == "batch_timeout"
    assert result[1]["protocol"] == "aave-v3"
    assert result[1]["error_type"] is None
