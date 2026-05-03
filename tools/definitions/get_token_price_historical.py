# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_token_price_historical – historical crypto price data via CoinGecko."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

from config import get_settings
from tools.definitions.get_token_price import _SYMBOL_TO_ID, _load_coins_list_index, _resolve_id
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── In-process TTL cache (per token + currency + window) ─────────────────────
# Keyed by "{cg_id}:{vs_currency}:{days}"; value is (expires_at, computed_entry).
# 10-minute TTL balances freshness against CoinGecko rate limits — historical
# tails are stable and the most-recent point shifts slowly relative to the
# window length.
_historical_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_HISTORICAL_CACHE_TTL = 600  # seconds

# Cap on returned daily series length, regardless of input window. Keeps
# response payload bounded so the agent context isn't flooded with raw points.
_MAX_DAILY_POINTS = 90


# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetTokenPriceHistoricalInput(BaseModel):
    tokens: list[str] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Token symbols or CoinGecko IDs (e.g. ['BTC', 'ETH']). Max 10 per call.",
    )
    days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Lookback window in days (1–365). 1=5-min granularity, 2-90=hourly, 91+=daily.",
    )
    vs_currency: str = Field(
        default="usd",
        pattern=r"^[a-z]{3,10}$",
        description="Quote currency (usd, eur, gbp, btc, eth). Lowercase 3–10 letters.",
    )
    stats_only: bool = Field(
        default=False,
        description=(
            "If true, omit the daily price series and return only period stats "
            "(start/end/change/high/low) to reduce payload size."
        ),
    )


class DailyPricePoint(BaseModel):
    date: str
    price: float


class TokenHistoricalEntry(BaseModel):
    id: str
    symbol: str
    price_start: float | None
    price_end: float | None
    price_change_pct: float | None
    price_high: float | None
    price_low: float | None
    daily_prices: list[DailyPricePoint]


class GetTokenPriceHistoricalOutput(BaseModel):
    vs_currency: str
    days: int
    tokens: list[TokenHistoricalEntry]


# ─── Implementation ──────────────────────────────────────────────────────────


async def _fetch_market_chart(cg_id: str, vs: str, days: int) -> list[list[float]] | None:
    """Call CoinGecko /coins/{id}/market_chart for the given window.

    Returns the raw list of ``[timestamp_ms, price]`` pairs on success, ``None``
    on any HTTP or transport failure. The API key, if configured, is forwarded
    via the demo-tier header — never logged or surfaced in error returns.
    """
    base_url = get_settings().coingecko_api_url.rstrip("/")
    url = f"{base_url}/coins/{cg_id}/market_chart?vs_currency={vs}&days={days}"

    headers: dict[str, str] = {}
    try:
        api_key = get_settings().coingecko_api_key
        if api_key:
            headers["x-cg-demo-api-key"] = api_key
    except Exception:
        pass

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data: dict[str, Any] = await resp.json()
                    prices = data.get("prices")
                    if isinstance(prices, list):
                        return prices
                    return None
                logger.warning("CoinGecko market_chart returned status %d for %s", resp.status, cg_id)
    except Exception as exc:
        logger.warning("CoinGecko market_chart request failed for %s: %s", cg_id, type(exc).__name__)
    return None


def _downsample_to_daily(prices: list[list[float]]) -> list[dict[str, Any]]:
    """Collapse a raw [[ts_ms, price], ...] series to one point per UTC day.

    Keeps the *last* observation per day (closest to that day's close in UTC),
    then truncates to the most-recent ``_MAX_DAILY_POINTS`` entries.
    """
    by_day: dict[str, float] = {}
    for entry in prices:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        ts_ms, price = entry[0], entry[1]
        try:
            day = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc).date().isoformat()
        except (ValueError, OSError, TypeError):
            continue
        # Last write wins → preserves end-of-day price for sub-daily inputs.
        by_day[day] = float(price)

    sorted_days = sorted(by_day.items())
    if len(sorted_days) > _MAX_DAILY_POINTS:
        sorted_days = sorted_days[-_MAX_DAILY_POINTS:]
    return [{"date": d, "price": p} for d, p in sorted_days]


def _summarize(prices: list[list[float]] | None) -> dict[str, Any]:
    """Compute period statistics + downsampled daily series from raw prices."""
    empty = {
        "price_start": None,
        "price_end": None,
        "price_change_pct": None,
        "price_high": None,
        "price_low": None,
        "daily_prices": [],
    }
    if not prices:
        return empty

    numeric: list[float] = []
    for entry in prices:
        if isinstance(entry, list) and len(entry) >= 2:
            try:
                numeric.append(float(entry[1]))
            except (TypeError, ValueError):
                continue
    if not numeric:
        return empty

    start = numeric[0]
    end = numeric[-1]
    change_pct = ((end - start) / start * 100.0) if start else None

    return {
        "price_start": start,
        "price_end": end,
        "price_change_pct": change_pct,
        "price_high": max(numeric),
        "price_low": min(numeric),
        "daily_prices": _downsample_to_daily(prices),
    }


async def get_token_price_historical(
    tokens: list[str],
    days: int = 30,
    vs_currency: str = "usd",
    stats_only: bool = False,
) -> dict[str, Any]:
    """Get historical prices and trend statistics for one or more crypto tokens."""
    vs = vs_currency.strip().lower()
    ids = [_resolve_id(t) for t in tokens]

    # Resolve any unknown symbols against the full CoinGecko coins list. Reuses
    # the 24-hour cached index from get_token_price to avoid redundant fetches.
    unresolved = [i for i, t in enumerate(tokens) if t.strip().lower() not in _SYMBOL_TO_ID]
    if unresolved:
        coin_map = await _load_coins_list_index()
        for i in unresolved:
            key = tokens[i].strip().lower()
            ids[i] = coin_map.get(key, ids[i])

    now = time.monotonic()

    # Split into cached vs uncached — only fetch what we don't already have.
    summaries: dict[str, dict[str, Any]] = {}
    missing_ids: list[str] = []

    for cg_id in ids:
        cache_key = f"{cg_id}:{vs}:{days}"
        entry = _historical_cache.get(cache_key)
        if entry and now < entry[0]:
            summaries[cg_id] = entry[1]
        else:
            if cg_id not in missing_ids:
                missing_ids.append(cg_id)

    if missing_ids:
        # Fan out concurrently; each token requires its own market_chart call.
        raw_results = await asyncio.gather(
            *(_fetch_market_chart(cg_id, vs, days) for cg_id in missing_ids),
            return_exceptions=False,
        )
        expires_at = now + _HISTORICAL_CACHE_TTL
        for cg_id, raw in zip(missing_ids, raw_results):
            summary = _summarize(raw)
            _historical_cache[f"{cg_id}:{vs}:{days}"] = (expires_at, summary)
            summaries[cg_id] = summary

    token_entries = [
        {
            "id": cg_id,
            "symbol": original.upper(),
            **summaries.get(cg_id, _summarize(None)),
        }
        for original, cg_id in zip(tokens, ids)
    ]

    if stats_only:
        for entry in token_entries:
            entry["daily_prices"] = []

    return {"vs_currency": vs, "days": days, "tokens": token_entries}


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_token_price_historical",
    version="1.0.0",
    description=(
        "Get historical price data for crypto tokens over a specified time window (1–365 days). "
        "Returns period statistics (start, end, % change, high, low) plus a downsampled daily "
        "price series. Use for period comparisons (month-over-month, YTD), trend analysis, and "
        "price charts. Prefer over web_search for any time-comparative financial query. Pass "
        "stats_only=true when the daily series is unnecessary. price_change_pct is pre-computed "
        "and should not be re-derived with calculate."
    ),
    tags=["finance", "crypto", "price", "history", "market"],
    input_schema=GetTokenPriceHistoricalInput,
    output_schema=GetTokenPriceHistoricalOutput,
    implementation=get_token_price_historical,
)
