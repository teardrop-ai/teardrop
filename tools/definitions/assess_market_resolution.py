# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""assess_market_resolution – Composite Polymarket resolution-risk verdict."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, Field, field_validator

from tools._internals._http_session import get_polymarket_session
from tools._internals.provenance import DataProvenance, cache_age_seconds, utc_now_iso
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
_POLYMARKET_HOSTS = frozenset({"polymarket.com", "www.polymarket.com"})
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,198}[a-z0-9])?$")
_ID_RE = re.compile(r"^[0-9]{1,12}$")
_CACHE_TTL = 120
_CACHE_MAX_ENTRIES = 512
_MAX_TEXT_CHARS = 300
_MAX_OUTCOMES = 10
_MIN_RULES_CHARS = 120
_THIN_LIQUIDITY_USD = 1_000.0
_NO_LIQUIDITY_USD = 100.0
_WIDE_SPREAD = 0.05

_DISCRETION_MARKERS = ("sole discretion", "at the discretion", "at its discretion", "reserves the right")
_CONSENSUS_MARKERS = ("consensus of credible reporting", "credible reporting", "widely reported", "media reports")
_FIFTY_FIFTY_MARKERS = ("50-50", "50/50", "fifty-fifty")
_CLARIFICATION_MARKERS = ("additional context", "clarification")
_PRICE_FEED_MARKERS = ("chainlink", "pyth", "price feed", "candle", "close price", "closing price")
_OFFICIAL_MARKERS = ("official", "according to", "as reported by", "announced by", "resolution source")

_SEVERITY_ORDER: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
_VERDICT_CATEGORIES = frozenset({"rules", "resolution"})

_market_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class MarketNotFoundError(ValueError):
    """Raised when Polymarket has no market for the requested identifier."""


# ─── Schemas ──────────────────────────────────────────────────────────────────


class AssessMarketResolutionInput(BaseModel):
    market: str = Field(
        ...,
        description=(
            "Polymarket market slug (e.g. 'will-bitcoin-reach-150k-in-2026'), numeric Gamma market id, "
            "or a polymarket.com market URL (the last path segment is used)."
        ),
    )

    @field_validator("market")
    @classmethod
    def _normalize_market(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned.lower().startswith(("http://", "https://")):
            parts = urlsplit(cleaned)
            if (parts.hostname or "").lower() not in _POLYMARKET_HOSTS:
                raise ValueError("market URL must be a polymarket.com URL")
            segments = [segment for segment in parts.path.split("/") if segment]
            if not segments:
                raise ValueError("market URL must include a market slug")
            cleaned = segments[-1]
        cleaned = cleaned.lower()
        if _ID_RE.fullmatch(cleaned) or _SLUG_RE.fullmatch(cleaned):
            return cleaned
        raise ValueError("market must be a Polymarket market slug, numeric market id, or polymarket.com URL")


class MarketRiskFactor(BaseModel):
    code: str
    category: Literal["state", "resolution", "rules", "execution"]
    severity: Literal["high", "medium", "low"]
    detail: str


class AssessMarketResolutionOutput(BaseModel):
    market_id: str
    slug: str
    question: str
    verdict: Literal["tradeable", "ambiguous", "avoid", "insufficient_data"]
    verdict_reason: str
    resolution_basis: Literal["price_feed", "official_source", "media_consensus", "unspecified"]
    resolution_source: str = ""
    uma_status: str = ""
    disputed: bool = False
    rule_flags: list[str] = Field(default_factory=list)
    risk_factors: list[MarketRiskFactor] = Field(default_factory=list)
    end_date: str | None = None
    hours_to_end: float | None = None
    outcome_prices: dict[str, float] = Field(default_factory=dict)
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    liquidity_usd: float | None = None
    neg_risk: bool = False
    provenance: DataProvenance
    as_of: str


# ─── Parsing helpers ──────────────────────────────────────────────────────────


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_list(value: Any) -> list[Any]:
    """Gamma encodes several array fields as JSON strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _text(value: Any, limit: int = _MAX_TEXT_CHARS) -> str:
    return str(value or "").strip()[:limit]


def _matches(text: str, markers: tuple[str, ...]) -> list[str]:
    return [marker for marker in markers if marker in text]


# ─── Data fetch ───────────────────────────────────────────────────────────────


def _market_url(market: str) -> str:
    path = f"/markets/{market}" if _ID_RE.fullmatch(market) else f"/markets/slug/{market}"
    return f"{_GAMMA_BASE_URL}{path}"


async def _fetch_market(market: str) -> dict[str, Any]:
    session = await get_polymarket_session()
    async with session.get(_market_url(market), timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status == 404:
            raise MarketNotFoundError(f"Polymarket market '{market}' was not found; pass a market slug, not an event slug")
        if resp.status != 200:
            raise RuntimeError(f"Polymarket Gamma API returned status {resp.status}")
        payload = await resp.json(content_type=None)
    if not isinstance(payload, dict):
        raise ValueError("Invalid Polymarket Gamma market response format")
    return payload


# ─── Risk & verdict evaluation ────────────────────────────────────────────────


def _evaluate_market(market: dict[str, Any], now: datetime, source_url: str) -> AssessMarketResolutionOutput:
    factors: list[MarketRiskFactor] = []

    def add(code: str, category: str, severity: str, detail: str) -> None:
        factors.append(MarketRiskFactor(code=code, category=category, severity=severity, detail=detail))

    rules = str(market.get("description") or "").strip()
    rules_lower = rules.lower()
    resolution_source = _text(market.get("resolutionSource"))

    # Market state
    if market.get("archived") is True or market.get("closed") is True:
        add("market_closed", "state", "high", "Market is closed or archived; no further trading is possible.")
    elif market.get("active") is False or market.get("acceptingOrders") is False or market.get("enableOrderBook") is False:
        add("not_accepting_orders", "state", "high", "Market order book is not accepting orders.")

    # UMA resolution state
    uma_status = _text(market.get("umaResolutionStatus"), 40).lower()
    history = [str(status).strip().lower() for status in _json_list(market.get("umaResolutionStatuses"))]
    disputed = uma_status == "disputed" or "disputed" in history
    if uma_status == "disputed":
        add(
            "resolution_disputed",
            "resolution",
            "high",
            "A UMA resolution proposal is under dispute; settlement can take 4-6 days and may resolve 50-50.",
        )
    elif disputed:
        add("prior_dispute", "resolution", "medium", "A previous UMA resolution proposal for this market was disputed.")
    if uma_status == "proposed":
        add(
            "resolution_proposed",
            "resolution",
            "medium",
            "An outcome has been proposed to UMA and is inside its challenge window; the price may already reflect it.",
        )

    # Timing
    end = _parse_time(market.get("endDate")) or _parse_time(market.get("endDateIso"))
    hours_to_end = round((end - now).total_seconds() / 3600, 2) if end else None
    if end is None:
        add("end_date_missing", "rules", "low", "Market publishes no end date.")
    elif end <= now and market.get("closed") is not True:
        add("past_end_date", "resolution", "medium", "End date has passed and the market is awaiting resolution.")

    # Rule wording
    discretion = _matches(rules_lower, _DISCRETION_MARKERS)
    consensus = _matches(rules_lower, _CONSENSUS_MARKERS)
    fifty_fifty = _matches(rules_lower, _FIFTY_FIFTY_MARKERS)
    clarified = _matches(rules_lower, _CLARIFICATION_MARKERS)
    price_feed = _matches(rules_lower, _PRICE_FEED_MARKERS)
    official = _matches(rules_lower, _OFFICIAL_MARKERS)

    if not rules:
        add("rules_missing", "rules", "high", "Market rules text is empty; the outcome definition cannot be checked.")
    elif len(rules) < _MIN_RULES_CHARS:
        add("rules_thin", "rules", "medium", "Market rules are very short and may not cover edge cases.")
    if discretion:
        add("discretionary_resolution", "rules", "high", "Rules allow discretionary resolution by the operator.")
    if fifty_fifty:
        add("fifty_fifty_fallback", "rules", "medium", "Rules include a 50-50 resolution fallback for edge cases.")
    if clarified:
        add("rules_clarified", "rules", "medium", "Rules carry an added clarification; read the latest wording.")
    if market.get("negRiskOther") is True:
        add(
            "neg_risk_other",
            "rules",
            "medium",
            "Market is the 'Other' placeholder of a negative-risk group and resolves on the absence of listed outcomes.",
        )

    if price_feed:
        resolution_basis = "price_feed"
    elif resolution_source:
        resolution_basis = "official_source"
    elif consensus:
        resolution_basis = "media_consensus"
    elif official:
        resolution_basis = "official_source"
    else:
        resolution_basis = "unspecified"
    if resolution_basis == "media_consensus":
        add("media_consensus_source", "rules", "medium", "Outcome is determined by a consensus of media reporting.")
    elif resolution_basis == "unspecified" and rules:
        add("no_explicit_source", "rules", "medium", "Rules name no explicit resolution source.")

    # Executability
    liquidity = _as_float(market.get("liquidityNum"))
    if liquidity is None:
        liquidity = _as_float(market.get("liquidity"))
    best_bid = _as_float(market.get("bestBid"))
    best_ask = _as_float(market.get("bestAsk"))
    spread = _as_float(market.get("spread"))
    if spread is None and best_bid is not None and best_ask is not None:
        spread = round(best_ask - best_bid, 6)
    if liquidity is not None and liquidity < _NO_LIQUIDITY_USD:
        add("no_liquidity", "execution", "high", f"Reported liquidity is ${liquidity:,.0f}; orders are unlikely to fill.")
    elif liquidity is not None and liquidity < _THIN_LIQUIDITY_USD:
        add("thin_liquidity", "execution", "medium", f"Reported liquidity is ${liquidity:,.0f}; expect slippage.")
    if spread is not None and spread > _WIDE_SPREAD:
        add("wide_spread", "execution", "medium", f"Bid-ask spread is {spread:.3f}; the midpoint is a weak probability signal.")
    if market.get("restricted") is True:
        add("geo_restricted", "state", "low", "Market is geo-restricted in some jurisdictions.")

    factors.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], f.category, f.code))

    if any(f.severity == "high" and f.category in {"state", "resolution", "execution"} for f in factors):
        verdict = "avoid"
        verdict_reason = next(f.detail for f in factors if f.severity == "high" and f.category != "rules")
    elif not rules and not resolution_source:
        verdict = "insufficient_data"
        verdict_reason = "Market rules and resolution source are unavailable; resolution risk cannot be assessed."
    elif any(f.severity == "high" and f.category == "rules" for f in factors) or (
        sum(f.severity == "medium" and f.category in _VERDICT_CATEGORIES for f in factors) >= 2
    ):
        verdict = "ambiguous"
        verdict_reason = next(f.detail for f in factors if f.category in _VERDICT_CATEGORIES)
    else:
        verdict = "tradeable"
        verdict_reason = "Market is open, undisputed, and its rules name a resolution basis."

    labels = [str(label) for label in _json_list(market.get("outcomes"))]
    prices = [_as_float(price) for price in _json_list(market.get("outcomePrices"))]
    outcome_prices = {label[:80]: price for label, price in list(zip(labels, prices))[:_MAX_OUTCOMES] if price is not None}

    return AssessMarketResolutionOutput(
        market_id=_text(market.get("id"), 40),
        slug=_text(market.get("slug"), 200),
        question=_text(market.get("question")),
        verdict=verdict,
        verdict_reason=verdict_reason,
        resolution_basis=resolution_basis,
        resolution_source=resolution_source,
        uma_status=uma_status,
        disputed=disputed,
        rule_flags=sorted(set(discretion + consensus + fifty_fifty + clarified + price_feed)),
        risk_factors=factors,
        end_date=end.isoformat() if end else None,
        hours_to_end=hours_to_end,
        outcome_prices=outcome_prices,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        liquidity_usd=liquidity,
        neg_risk=market.get("negRisk") is True,
        provenance=DataProvenance(
            provider="Polymarket Gamma API",
            source_urls=[source_url],
            retrieved_at=utc_now_iso(),
            cache_hit=False,
            cache_ttl_seconds=_CACHE_TTL,
        ),
        as_of=utc_now_iso(),
    )


# ─── Tool implementation ──────────────────────────────────────────────────────


def _cache_get(key: str) -> dict[str, Any] | None:
    entry = _market_cache.get(key)
    if entry is None or time.monotonic() >= entry[0]:
        return None
    result = dict(entry[1])
    provenance = dict(result["provenance"])
    provenance["cache_hit"] = True
    provenance["cache_age_seconds"] = cache_age_seconds(provenance.get("retrieved_at"))
    result["provenance"] = provenance
    return result


def _cache_put(key: str, result: dict[str, Any]) -> None:
    if key not in _market_cache and len(_market_cache) >= _CACHE_MAX_ENTRIES:
        _market_cache.pop(next(iter(_market_cache)))
    _market_cache[key] = (time.monotonic() + _CACHE_TTL, result)


async def assess_market_resolution(market: str) -> dict[str, Any]:
    """Assess whether a Polymarket market's rules and resolution state make it safe to trade."""
    input_data = AssessMarketResolutionInput(market=market)
    cached = _cache_get(input_data.market)
    if cached is not None:
        return cached

    payload = await _fetch_market(input_data.market)
    output = _evaluate_market(payload, datetime.now(timezone.utc), _market_url(input_data.market))
    result = output.model_dump()
    _cache_put(input_data.market, result)
    return result


TOOL = ToolDefinition(
    name="assess_market_resolution",
    version="1.0.0",
    description=(
        "Return a decision-ready resolution-risk verdict for one Polymarket market before an agent trades, "
        "quotes, or forecasts it. The result branches to tradeable, ambiguous, avoid, or insufficient_data and "
        "names the resolution basis (price_feed, official_source, media_consensus, unspecified), UMA dispute "
        "state, rule-wording risks, and executability (liquidity, spread) so the calling agent can act without "
        "reading the full rules."
    ),
    use_when=(
        "Call once per market before placing, quoting, or forecasting a Polymarket position when the caller needs "
        "to know whether the rules and resolution state make the outcome well-defined. Repeat calls within two "
        "minutes return cached data."
    ),
    limitations=(
        "Heuristic analysis of Polymarket rule wording and Gamma API state; not a probability forecast, legal "
        "advice, or a guarantee of how UMA voters resolve. Does not read order-book depth, event-level "
        "augmented negative-risk settings, or clarifications published off-platform."
    ),
    alternatives=["web_search", "http_fetch"],
    tags=["prediction-markets", "polymarket", "resolution", "risk", "uma", "decision"],
    examples=[
        "Is this Polymarket market safe to trade, or are its resolution rules ambiguous?",
        "Check whether this prediction market is disputed before I place an order.",
        "Give me a tradeable, ambiguous, or avoid verdict for this Polymarket market.",
    ],
    input_schema=AssessMarketResolutionInput,
    output_schema=AssessMarketResolutionOutput,
    implementation=assess_market_resolution,
)
