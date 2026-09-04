# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for tools/definitions/validate_opportunity.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from tools.definitions.validate_opportunity import (
    OpportunityValidationError,
    ValidateOpportunityInput,
    validate_opportunity,
)

_VALID_POOL_ID = "747c1d2a-c668-4682-b9f9-296708a3dd90"


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setattr("tools.definitions.validate_opportunity._opportunity_cache", {})
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_coingecko_stable_price",
        AsyncMock(return_value=1.0),
    )


def _healthy_pool_data():
    return {
        "pool": _VALID_POOL_ID,
        "project": "aave-v3",
        "symbol": "USDC",
        "chain": "Ethereum",
        "tvlUsd": 25_000_000.0,
        "apy": 4.5,
        "apyMean7d": 4.6,
        "apyMean30d": 4.4,
        "apyBase": 4.2,
        "apyReward": 0.3,
        "stablecoin": True,
        "ilRisk": "no",
    }


def _healthy_chart_data():
    return [{"timestamp": f"2026-08-{i:02d}", "tvlUsd": 24_000_000.0 + (i * 30_000.0), "apy": 4.5} for i in range(1, 32)]


@pytest.mark.anyio
async def test_validate_opportunity_happy_path_sustainable(monkeypatch):
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_snapshot",
        AsyncMock(return_value=_healthy_pool_data()),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_chart",
        AsyncMock(return_value=_healthy_chart_data()),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_coingecko_stable_price",
        AsyncMock(return_value=1.0),
    )

    result = await validate_opportunity(pool_id=_VALID_POOL_ID)

    assert result["pool_id"] == _VALID_POOL_ID
    assert result["project"] == "aave-v3"
    assert result["symbol"] == "USDC"
    assert result["chain"] == "Ethereum"
    assert result["verdict"] == "sustainable"
    assert result["data_complete"] is True
    assert result["partial_errors"] == []
    assert len(result["risk_factors"]) == 0
    assert result["yield_summary"]["apy"] == 4.5
    assert result["yield_summary"]["is_spiking"] is False
    assert result["liquidity_summary"]["tvl_usd"] == 25_000_000.0
    assert result["liquidity_summary"]["drain_detected"] is False
    assert result["provenance"]["provider"] == "DeFiLlama & CoinGecko"
    assert result["provenance"]["cache_hit"] is False


@pytest.mark.anyio
async def test_validate_opportunity_caution_high_apy(monkeypatch):
    pool_data = _healthy_pool_data()
    pool_data["apy"] = 125.0
    pool_data["apyBase"] = 5.0
    pool_data["apyReward"] = 120.0
    pool_data["apyMean30d"] = 120.0

    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_snapshot",
        AsyncMock(return_value=pool_data),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_chart",
        AsyncMock(return_value=_healthy_chart_data()),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_coingecko_stable_price",
        AsyncMock(return_value=1.0),
    )

    result = await validate_opportunity(pool_id=_VALID_POOL_ID)

    assert result["verdict"] == "caution"
    assert any(rf["category"] == "yield" and rf["severity"] == "high" for rf in result["risk_factors"])


@pytest.mark.anyio
async def test_validate_opportunity_caution_reward_heavy(monkeypatch):
    pool_data = _healthy_pool_data()
    pool_data["apy"] = 25.0
    pool_data["apyBase"] = 2.0
    pool_data["apyReward"] = 23.0
    pool_data["apyMean30d"] = 24.0

    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_snapshot",
        AsyncMock(return_value=pool_data),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_chart",
        AsyncMock(return_value=_healthy_chart_data()),
    )

    result = await validate_opportunity(pool_id=_VALID_POOL_ID)

    assert result["verdict"] == "caution"
    assert any("emission-dependent" in rf["detail"] for rf in result["risk_factors"])


@pytest.mark.anyio
async def test_validate_opportunity_caution_spiking_apy(monkeypatch):
    pool_data = _healthy_pool_data()
    pool_data["apy"] = 18.0
    pool_data["apyMean30d"] = 4.0

    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_snapshot",
        AsyncMock(return_value=pool_data),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_chart",
        AsyncMock(return_value=_healthy_chart_data()),
    )

    result = await validate_opportunity(pool_id=_VALID_POOL_ID)

    assert result["verdict"] == "caution"
    assert result["yield_summary"]["is_spiking"] is True
    assert any("3x or higher" in rf["detail"] for rf in result["risk_factors"])


@pytest.mark.anyio
async def test_validate_opportunity_unsustainable_low_tvl(monkeypatch):
    pool_data = _healthy_pool_data()
    pool_data["tvlUsd"] = 45_000.0

    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_snapshot",
        AsyncMock(return_value=pool_data),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_chart",
        AsyncMock(return_value=_healthy_chart_data()),
    )

    result = await validate_opportunity(pool_id=_VALID_POOL_ID)

    assert result["verdict"] == "unsustainable"
    assert any("Critically low TVL" in rf["detail"] for rf in result["risk_factors"])


@pytest.mark.anyio
async def test_validate_opportunity_unsustainable_severe_drain(monkeypatch):
    chart_data = [
        {"timestamp": "2026-08-01", "tvlUsd": 20_000_000.0, "apy": 5.0},
        {"timestamp": "2026-08-15", "tvlUsd": 12_000_000.0, "apy": 4.5},
        {"timestamp": "2026-08-31", "tvlUsd": 5_000_000.0, "apy": 4.0},
    ]

    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_snapshot",
        AsyncMock(return_value=_healthy_pool_data()),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_chart",
        AsyncMock(return_value=chart_data),
    )

    result = await validate_opportunity(pool_id=_VALID_POOL_ID)

    assert result["verdict"] == "unsustainable"
    assert result["liquidity_summary"]["drain_detected"] is True
    assert any("Severe TVL contraction" in rf["detail"] for rf in result["risk_factors"])


@pytest.mark.anyio
async def test_validate_opportunity_unsustainable_extreme_apy(monkeypatch):
    pool_data = _healthy_pool_data()
    pool_data["apy"] = 280.0

    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_snapshot",
        AsyncMock(return_value=pool_data),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_chart",
        AsyncMock(return_value=_healthy_chart_data()),
    )

    result = await validate_opportunity(pool_id=_VALID_POOL_ID)

    assert result["verdict"] == "unsustainable"


@pytest.mark.anyio
async def test_validate_opportunity_stablecoin_depeg(monkeypatch):
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_snapshot",
        AsyncMock(return_value=_healthy_pool_data()),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_chart",
        AsyncMock(return_value=_healthy_chart_data()),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_coingecko_stable_price",
        AsyncMock(return_value=0.91),
    )

    result = await validate_opportunity(pool_id=_VALID_POOL_ID)

    assert any(rf["category"] == "peg_stability" and rf["severity"] == "high" for rf in result["risk_factors"])


def test_validate_opportunity_input_validation():
    with pytest.raises(ValidationError):
        ValidateOpportunityInput(pool_id="not-a-valid-uuid")

    with pytest.raises(ValidationError):
        ValidateOpportunityInput(pool_id="../../../etc/passwd")

    valid = ValidateOpportunityInput(pool_id=_VALID_POOL_ID.upper())
    assert valid.pool_id == _VALID_POOL_ID.lower()


@pytest.mark.anyio
async def test_validate_opportunity_partial_failure_chart(monkeypatch):
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_snapshot",
        AsyncMock(return_value=_healthy_pool_data()),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_chart",
        AsyncMock(side_effect=RuntimeError("DeFiLlama chart 500")),
    )

    result = await validate_opportunity(pool_id=_VALID_POOL_ID)

    assert result["verdict"] == "insufficient_data"
    assert result["data_complete"] is False
    assert len(result["partial_errors"]) == 1
    assert "chart history failed" in result["partial_errors"][0]


@pytest.mark.anyio
async def test_validate_opportunity_all_fail_raises(monkeypatch):
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_snapshot",
        AsyncMock(side_effect=RuntimeError("DeFiLlama pool 503")),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_chart",
        AsyncMock(side_effect=RuntimeError("DeFiLlama chart 503")),
    )

    with pytest.raises(OpportunityValidationError) as exc_info:
        await validate_opportunity(pool_id=_VALID_POOL_ID)

    assert "All opportunity validation data sources failed" in str(exc_info.value)


@pytest.mark.anyio
async def test_validate_opportunity_cache_hit(monkeypatch):
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_snapshot",
        AsyncMock(return_value=_healthy_pool_data()),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_chart",
        AsyncMock(return_value=_healthy_chart_data()),
    )

    res1 = await validate_opportunity(pool_id=_VALID_POOL_ID)
    assert res1["provenance"]["cache_hit"] is False

    res2 = await validate_opportunity(pool_id=_VALID_POOL_ID)
    assert res2["provenance"]["cache_hit"] is True


@pytest.mark.anyio
async def test_validate_opportunity_deterministic_risk_factor_order(monkeypatch):
    pool_data = _healthy_pool_data()
    pool_data["apy"] = 150.0  # high yield risk
    pool_data["tvlUsd"] = 500_000.0  # medium liquidity risk
    pool_data["ilRisk"] = "yes"  # low IL risk

    chart_data = [{"timestamp": f"2026-08-{i:02d}", "tvlUsd": 500_000.0, "apy": 150.0} for i in range(1, 5)]

    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_snapshot",
        AsyncMock(return_value=pool_data),
    )
    monkeypatch.setattr(
        "tools.definitions.validate_opportunity._fetch_pool_chart",
        AsyncMock(return_value=chart_data),
    )

    result = await validate_opportunity(pool_id=_VALID_POOL_ID)

    severities = [rf["severity"] for rf in result["risk_factors"]]
    # Expect: ["high", "medium", "medium", "low"]
    assert severities[0] == "high"
    assert all(s == "medium" for s in severities[1:-1])
    assert severities[-1] == "low"
