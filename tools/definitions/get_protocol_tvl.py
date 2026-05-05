# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_protocol_tvl – DeFi protocol TVL data via DeFiLlama."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
from pydantic import BaseModel, Field, field_validator

from tools.definitions._http_session import get_defillama_session
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── DeFiLlama API ────────────────────────────────────────────────────────────
# Free public API; no authentication required.
_DEFILLAMA_BASE_URL = "https://api.llama.fi"

# ─── In-process TTL cache ─────────────────────────────────────────────────────
# Keyed by "{slug}:{'hist' if include_historical else 'tvl'}:{days}"
# TVL numbers are updated hourly by DeFiLlama; 5-minute TTL is safe for agent use.
_tvl_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_TVL_CACHE_TTL = 300  # seconds

# Cap chain breakdown and historical series to keep agent context bounded.
_MAX_CHAIN_ENTRIES = 10
_MAX_HISTORICAL_POINTS = 90

# Valid protocol slug pattern — prevents path traversal / injection.
_SLUG_PATTERN = r"^[a-zA-Z0-9\-\_\.]{1,64}$"


# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetProtocolTvlInput(BaseModel):
    protocol: str = Field(
        ...,
        description=(
            "DeFiLlama protocol slug (e.g. 'aave-v3', 'uniswap-v3', 'curve-dex'). "
            "Use lowercase with hyphens as shown on DeFiLlama. "
            "Tip: 'aave' works for the combined Aave TVL; 'aave-v3' for V3 only."
        ),
    )
    include_historical: bool = Field(
        default=False,
        description="If true, return a daily historical TVL series for the requested window.",
    )
    days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Lookback window in days for the historical series (only used when include_historical=True).",
    )

    @field_validator("protocol")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        import re

        if not re.match(_SLUG_PATTERN, v):
            raise ValueError(
                "protocol must be a valid DeFiLlama slug: lowercase letters, "
                "digits, hyphens, underscores, or dots; max 64 characters."
            )
        return v.lower()


class ChainTvlEntry(BaseModel):
    chain: str
    tvl_usd: float


class DailyTvlPoint(BaseModel):
    date: str
    tvl_usd: float


class GetProtocolTvlOutput(BaseModel):
    protocol: str
    current_tvl_usd: float | None
    tvl_7d_change_pct: float | None
    tvl_30d_change_pct: float | None
    chain_breakdown: list[ChainTvlEntry]
    historical_series: list[DailyTvlPoint] | None
    note: str


# ─── HTTP helpers ─────────────────────────────────────────────────────────────


async def _fetch_current_tvl(slug: str) -> float | None:
    """Call GET /tvl/{slug} — returns a single float or None on failure."""
    url = f"{_DEFILLAMA_BASE_URL}/tvl/{slug}"
    try:
        session = await get_defillama_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                text = await resp.text()
                return float(text.strip())
            if resp.status == 404:
                logger.warning("DeFiLlama: protocol not found: %s", slug)
                return None
            logger.warning("DeFiLlama /tvl returned status %d for %s", resp.status, slug)
    except (ValueError, TypeError):
        logger.warning("DeFiLlama /tvl returned non-numeric response for %s", slug)
    except Exception as exc:
        logger.warning("DeFiLlama /tvl request failed for %s: %s", slug, type(exc).__name__)
    return None


async def _fetch_protocol_detail(slug: str) -> dict[str, Any] | None:
    """Call GET /protocol/{slug} — returns the full DeFiLlama protocol object or None."""
    url = f"{_DEFILLAMA_BASE_URL}/protocol/{slug}"
    try:
        session = await get_defillama_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 404:
                logger.warning("DeFiLlama: protocol detail not found: %s", slug)
                return None
            logger.warning("DeFiLlama /protocol returned status %d for %s", resp.status, slug)
    except Exception as exc:
        logger.warning("DeFiLlama /protocol request failed for %s: %s", slug, type(exc).__name__)
    return None


# ─── Data extraction helpers ──────────────────────────────────────────────────


def _extract_chain_breakdown(detail: dict[str, Any]) -> list[ChainTvlEntry]:
    """Extract per-chain TVL from the DeFiLlama protocol detail payload.

    ``chainTvls`` is a dict of {ChainName: {tvl: [{date, totalLiquidityUSD}]}}
    We take the last (most-recent) entry per chain, sort by TVL descending,
    and cap at _MAX_CHAIN_ENTRIES.
    """
    chain_tvls: dict[str, Any] = detail.get("chainTvls", {})
    entries: list[ChainTvlEntry] = []
    for chain_name, chain_data in chain_tvls.items():
        if not isinstance(chain_data, dict):
            continue
        tvl_series = chain_data.get("tvl", [])
        if not isinstance(tvl_series, list) or not tvl_series:
            continue
        last = tvl_series[-1]
        if not isinstance(last, dict):
            continue
        try:
            tvl_usd = float(last.get("totalLiquidityUSD", 0))
        except (TypeError, ValueError):
            continue
        if tvl_usd > 0:
            entries.append(ChainTvlEntry(chain=chain_name, tvl_usd=tvl_usd))

    entries.sort(key=lambda e: e.tvl_usd, reverse=True)
    return entries[:_MAX_CHAIN_ENTRIES]


def _extract_historical_series(detail: dict[str, Any], days: int) -> list[DailyTvlPoint]:
    """Extract a daily TVL series from the DeFiLlama ``tvl`` array.

    The ``tvl`` field is a list of ``{date: unix_timestamp, totalLiquidityUSD: float}``.
    We deduplicate to one-per-day (last entry wins), then slice to the requested
    window and cap at _MAX_HISTORICAL_POINTS.
    """
    raw: list[Any] = detail.get("tvl", [])
    if not isinstance(raw, list):
        return []

    by_day: dict[str, float] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        ts = entry.get("date")
        tvl = entry.get("totalLiquidityUSD")
        if ts is None or tvl is None:
            continue
        try:
            day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
            by_day[day] = float(tvl)
        except (ValueError, OSError, TypeError):
            continue

    sorted_days = sorted(by_day.items())

    # Trim to the requested lookback window.
    if days < 365:
        cutoff_idx = max(0, len(sorted_days) - days)
        sorted_days = sorted_days[cutoff_idx:]

    # Cap at the absolute max to keep response size bounded.
    if len(sorted_days) > _MAX_HISTORICAL_POINTS:
        sorted_days = sorted_days[-_MAX_HISTORICAL_POINTS:]

    return [DailyTvlPoint(date=d, tvl_usd=v) for d, v in sorted_days]


def _compute_change_pct(series: list[Any], days_ago: int) -> float | None:
    """Compute percentage change vs. N days ago from a sorted daily series.

    ``series`` is a list of ``[date, tvl_usd]`` tuples from ``by_day.items()``.
    """
    if not series or len(series) < 2:
        return None
    try:
        current = series[-1][1]
        target_idx = max(0, len(series) - days_ago - 1)
        past = series[target_idx][1]
        if past == 0:
            return None
        return (current - past) / past * 100.0
    except (TypeError, ValueError, IndexError):
        return None


# ─── Main implementation ──────────────────────────────────────────────────────


async def get_protocol_tvl(
    protocol: str,
    include_historical: bool = False,
    days: int = 30,
) -> dict[str, Any]:
    """Get TVL data for a DeFi protocol from DeFiLlama."""
    slug = protocol.strip().lower()
    cache_key = f"{slug}:{'hist' if include_historical else 'tvl'}:{days}"
    now = time.monotonic()

    cached = _tvl_cache.get(cache_key)
    if cached and now < cached[0]:
        return cached[1]

    if not include_historical:
        # Fast path: single lightweight endpoint.
        current_tvl = await _fetch_current_tvl(slug)
        if current_tvl is None:
            result: dict[str, Any] = GetProtocolTvlOutput(
                protocol=slug,
                current_tvl_usd=None,
                tvl_7d_change_pct=None,
                tvl_30d_change_pct=None,
                chain_breakdown=[],
                historical_series=None,
                note="Protocol not found or DeFiLlama unavailable.",
            ).model_dump()
            _tvl_cache[cache_key] = (now + 60, result)  # short TTL for error/not-found results
            return result

        result = GetProtocolTvlOutput(
            protocol=slug,
            current_tvl_usd=current_tvl,
            tvl_7d_change_pct=None,
            tvl_30d_change_pct=None,
            chain_breakdown=[],
            historical_series=None,
            note="TVL sourced from DeFiLlama. Chain breakdown requires include_historical=True.",
        ).model_dump()
    else:
        # Detail path: richer endpoint with chains + full history.
        detail = await _fetch_protocol_detail(slug)
        if detail is None:
            fallback_current_tvl = await _fetch_current_tvl(slug)
            if fallback_current_tvl is not None:
                result = GetProtocolTvlOutput(
                    protocol=slug,
                    current_tvl_usd=fallback_current_tvl,
                    tvl_7d_change_pct=None,
                    tvl_30d_change_pct=None,
                    chain_breakdown=[],
                    historical_series=None,
                    note=(
                        "Historical TVL details unavailable from DeFiLlama; returned current TVL from /tvl fallback. "
                        "Chain breakdown requires include_historical=True with a successful detail response."
                    ),
                ).model_dump()
                _tvl_cache[cache_key] = (now + 60, result)
                return result
            result = GetProtocolTvlOutput(
                protocol=slug,
                current_tvl_usd=None,
                tvl_7d_change_pct=None,
                tvl_30d_change_pct=None,
                chain_breakdown=[],
                historical_series=None,
                note="Protocol not found or DeFiLlama unavailable.",
            ).model_dump()
            _tvl_cache[cache_key] = (now + 60, result)  # short TTL for error/not-found results
            return result

        chain_breakdown = _extract_chain_breakdown(detail)
        historical_series = _extract_historical_series(detail, days)

        # Compute current TVL from the last historical point (most authoritative).
        current_tvl = historical_series[-1].tvl_usd if historical_series else None

        # Compute change percentages from the full available series (up to 90 days),
        # not from the window-trimmed historical_series. Using the trimmed series
        # would report a 7-day delta as tvl_30d_change_pct when days=7 is requested.
        full_series = _extract_historical_series(detail, 365)
        sorted_full = [(p.date, p.tvl_usd) for p in full_series]
        tvl_7d = _compute_change_pct(sorted_full, 7)
        tvl_30d = _compute_change_pct(sorted_full, 30)

        result = GetProtocolTvlOutput(
            protocol=slug,
            current_tvl_usd=current_tvl,
            tvl_7d_change_pct=tvl_7d,
            tvl_30d_change_pct=tvl_30d,
            chain_breakdown=chain_breakdown,
            historical_series=historical_series,
            note=(
                "TVL sourced from DeFiLlama. "
                f"Historical series covers up to {_MAX_HISTORICAL_POINTS} daily points. "
                "Chain breakdown shows top chains by current TVL."
            ),
        ).model_dump()

    _tvl_cache[cache_key] = (now + _TVL_CACHE_TTL, result)
    return result


# ─── Tool definition ──────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_protocol_tvl",
    version="1.0.0",
    description=(
        "Get Total Value Locked (TVL) data for a DeFi protocol from DeFiLlama. "
        "Returns current TVL in USD, 7-day and 30-day percentage change, and a "
        "per-chain breakdown. Set include_historical=True to also retrieve a daily "
        "TVL series for trend analysis. Supports 3,000+ protocols including Aave, "
        "Uniswap, Curve, Compound, Lido, MakerDAO, and more. "
        "Use the DeFiLlama slug format: 'aave-v3', 'uniswap-v3', 'curve-dex'."
    ),
    tags=["defi", "tvl", "finance", "protocol", "defillama"],
    input_schema=GetProtocolTvlInput,
    output_schema=GetProtocolTvlOutput,
    implementation=get_protocol_tvl,
)
