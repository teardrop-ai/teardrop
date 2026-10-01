# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for tools/definitions/assess_market_resolution.py."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from tools.definitions.assess_market_resolution import (
    AssessMarketResolutionInput,
    MarketNotFoundError,
    _evaluate_market,
    _fetch_market,
    _market_url,
    assess_market_resolution,
)

_NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
_SLUG = "will-bitcoin-reach-150k-by-december-31-2026"
_URL = _market_url(_SLUG)
_RULES = (
    'This market will resolve to "Yes" if the Binance 1 minute candle for BTCUSDT on December 31, 2026 '
    '12:00 ET has a final "Close" price of $150,000 or higher. Otherwise, this market will resolve to "No". '
    "The resolution source for this market is Binance."
)


def _market(**overrides):
    market = {
        "id": "703257",
        "slug": _SLUG,
        "question": "Will Bitcoin reach $150k by December 31, 2026?",
        "description": _RULES,
        "resolutionSource": "https://www.binance.com/en/trade/BTC_USDT",
        "endDate": "2026-12-31T17:00:00Z",
        "active": True,
        "closed": False,
        "archived": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "negRisk": False,
        "restricted": False,
        "umaResolutionStatus": None,
        "umaResolutionStatuses": "[]",
        "liquidityNum": 150_248.32,
        "bestBid": 0.08,
        "bestAsk": 0.09,
        "spread": 0.01,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.085", "0.915"]',
    }
    market.update(overrides)
    return market


def _codes(result) -> set[str]:
    return {factor.code for factor in result.risk_factors}


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setattr("tools.definitions.assess_market_resolution._market_cache", {})


# ─── Input normalization ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_SLUG, _SLUG),
        ("  Will-Bitcoin-Reach-150K  ", "will-bitcoin-reach-150k"),
        ("703257", "703257"),
        (f"https://polymarket.com/event/bitcoin-150k/{_SLUG}?tid=1", _SLUG),
        (f"https://www.polymarket.com/market/{_SLUG}", _SLUG),
    ],
)
def test_input_normalizes_slug_id_and_url(raw, expected):
    assert AssessMarketResolutionInput(market=raw).market == expected


@pytest.mark.parametrize(
    "raw",
    ["", "../markets/1", "slug with spaces", "https://evil.example/market/abc", "https://polymarket.com/", "-leading"],
)
def test_input_rejects_invalid_markets(raw):
    with pytest.raises(ValidationError):
        AssessMarketResolutionInput(market=raw)


def test_market_url_routes_ids_and_slugs():
    assert _market_url("703257") == "https://gamma-api.polymarket.com/markets/703257"
    assert _market_url(_SLUG) == f"https://gamma-api.polymarket.com/markets/slug/{_SLUG}"


# ─── Verdicts ─────────────────────────────────────────────────────────────────


def test_price_feed_market_is_tradeable():
    result = _evaluate_market(_market(), _NOW, _URL)

    assert result.verdict == "tradeable"
    assert result.resolution_basis == "price_feed"
    assert result.risk_factors == []
    assert result.outcome_prices == {"Yes": 0.085, "No": 0.915}
    assert result.hours_to_end is not None and result.hours_to_end > 0
    assert result.provenance.source_urls == [_URL]
    assert "candle" in result.rule_flags


def test_closed_market_is_avoid():
    result = _evaluate_market(_market(closed=True, acceptingOrders=False), _NOW, _URL)

    assert result.verdict == "avoid"
    assert "market_closed" in _codes(result)


def test_not_accepting_orders_is_avoid():
    result = _evaluate_market(_market(acceptingOrders=False), _NOW, _URL)

    assert result.verdict == "avoid"
    assert "not_accepting_orders" in _codes(result)


def test_active_dispute_is_avoid():
    result = _evaluate_market(
        _market(umaResolutionStatus="disputed", umaResolutionStatuses='["proposed", "disputed"]'), _NOW, _URL
    )

    assert result.verdict == "avoid"
    assert result.disputed is True
    assert result.uma_status == "disputed"
    assert "resolution_disputed" in _codes(result)


def test_no_liquidity_is_avoid():
    result = _evaluate_market(_market(liquidityNum=50.0), _NOW, _URL)

    assert result.verdict == "avoid"
    assert "no_liquidity" in _codes(result)


def test_discretionary_rules_are_ambiguous():
    rules = _RULES + " Polymarket reserves the right to resolve this market at its sole discretion."
    result = _evaluate_market(_market(description=rules), _NOW, _URL)

    assert result.verdict == "ambiguous"
    assert "discretionary_resolution" in _codes(result)
    assert "sole discretion" in result.rule_flags


def test_media_consensus_with_prior_dispute_is_ambiguous():
    rules = (
        'This market will resolve to "Yes" if the named official resigns before the end date. The primary '
        "resolution source will be a consensus of credible reporting from major news outlets covering the story."
    )
    result = _evaluate_market(
        _market(
            description=rules,
            resolutionSource="",
            umaResolutionStatus="proposed",
            umaResolutionStatuses='["disputed", "proposed"]',
        ),
        _NOW,
        _URL,
    )

    assert result.resolution_basis == "media_consensus"
    assert result.verdict == "ambiguous"
    assert {"media_consensus_source", "prior_dispute", "resolution_proposed"} <= _codes(result)
    assert result.disputed is True


def test_single_medium_rules_factor_stays_tradeable():
    rules = _RULES + " If the market is cancelled it will resolve 50-50."
    result = _evaluate_market(_market(description=rules), _NOW, _URL)

    assert result.verdict == "tradeable"
    assert "fifty_fifty_fallback" in _codes(result)


def test_execution_mediums_do_not_change_verdict():
    result = _evaluate_market(_market(liquidityNum=500.0, spread=0.2), _NOW, _URL)

    assert result.verdict == "tradeable"
    assert {"thin_liquidity", "wide_spread"} <= _codes(result)


def test_missing_rules_and_source_is_insufficient_data():
    result = _evaluate_market(_market(description="", resolutionSource=""), _NOW, _URL)

    assert result.verdict == "insufficient_data"
    assert "rules_missing" in _codes(result)


def test_closed_market_without_rules_is_avoid_not_insufficient():
    result = _evaluate_market(_market(description="", resolutionSource="", closed=True), _NOW, _URL)

    assert result.verdict == "avoid"


def test_past_end_date_awaiting_resolution_flagged():
    result = _evaluate_market(_market(endDate="2026-09-01T00:00:00Z"), _NOW, _URL)

    assert "past_end_date" in _codes(result)
    assert result.hours_to_end is not None and result.hours_to_end < 0


def test_unspecified_source_flagged():
    rules = 'This market will resolve to "Yes" if the team wins the championship game this season, otherwise "No". ' * 2
    result = _evaluate_market(_market(description=rules, resolutionSource=""), _NOW, _URL)

    assert result.resolution_basis == "unspecified"
    assert "no_explicit_source" in _codes(result)


def test_minor_flags_and_clarified_other_outcome_are_ambiguous():
    rules = _RULES + " Additional context: trades on the delisted pair do not count."
    result = _evaluate_market(
        _market(description=rules, endDate=None, negRiskOther=True, restricted=True, negRisk=True), _NOW, _URL
    )

    assert {"rules_clarified", "neg_risk_other", "end_date_missing", "geo_restricted"} <= _codes(result)
    assert result.verdict == "ambiguous"
    assert result.neg_risk is True
    assert result.end_date is None and result.hours_to_end is None


def test_malformed_numeric_and_json_fields_are_tolerated():
    result = _evaluate_market(
        _market(liquidityNum="n/a", liquidity=None, bestBid=True, spread=None, outcomes="not-json", outcomePrices=None),
        _NOW,
        _URL,
    )

    assert result.liquidity_usd is None
    assert result.best_bid is None
    assert result.outcome_prices == {}


def test_risk_factor_order_is_deterministic():
    rules = "Short rules. Resolves 50-50 at the sole discretion of the operator."
    result = _evaluate_market(_market(description=rules, resolutionSource="", liquidityNum=500.0), _NOW, _URL)

    severities = [factor.severity for factor in result.risk_factors]
    assert severities == sorted(severities, key={"high": 0, "medium": 1, "low": 2}.get)


# ─── Tool implementation ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_tool_fetches_once_then_serves_cache(monkeypatch):
    fetch = AsyncMock(return_value=_market())
    monkeypatch.setattr("tools.definitions.assess_market_resolution._fetch_market", fetch)

    first = await assess_market_resolution(market=_SLUG)
    second = await assess_market_resolution(market=f"https://polymarket.com/market/{_SLUG}")

    fetch.assert_awaited_once_with(_SLUG)
    assert first["verdict"] == "tradeable"
    assert first["provenance"]["cache_hit"] is False
    assert second["provenance"]["cache_hit"] is True
    assert second["verdict"] == first["verdict"]


@pytest.mark.anyio
async def test_tool_propagates_not_found(monkeypatch):
    monkeypatch.setattr(
        "tools.definitions.assess_market_resolution._fetch_market",
        AsyncMock(side_effect=MarketNotFoundError("missing")),
    )

    with pytest.raises(MarketNotFoundError):
        await assess_market_resolution(market=_SLUG)


class _FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.urls: list[str] = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return self.response


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [(404, {}, MarketNotFoundError), (500, {}, RuntimeError), (200, ["not", "a", "market"], ValueError)],
)
async def test_fetch_market_error_paths(monkeypatch, status, payload, error):
    session = _FakeSession(_FakeResponse(status, payload))
    monkeypatch.setattr("tools.definitions.assess_market_resolution.get_polymarket_session", AsyncMock(return_value=session))

    with pytest.raises(error):
        await _fetch_market(_SLUG)
    assert session.urls == [_URL]


@pytest.mark.anyio
async def test_fetch_market_returns_payload(monkeypatch):
    session = _FakeSession(_FakeResponse(200, _market()))
    monkeypatch.setattr("tools.definitions.assess_market_resolution.get_polymarket_session", AsyncMock(return_value=session))

    assert (await _fetch_market(_SLUG))["id"] == "703257"


def test_tool_registered():
    from tools.definitions import _ALL_TOOLS

    assert "assess_market_resolution" in {tool.name for tool in _ALL_TOOLS}
