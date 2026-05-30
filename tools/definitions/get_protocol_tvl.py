# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_protocol_tvl – DeFi protocol TVL data via DeFiLlama."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
from pydantic import BaseModel, Field, field_validator, model_validator

from tools._internals._http_session import get_defillama_session
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
_RETRY_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.5
_PROTOCOL_DETAIL_TIMEOUT_SECONDS = 8
_BATCH_TIMEOUT_SECONDS = 25

# Valid protocol slug pattern — prevents path traversal / injection.
_SLUG_PATTERN = r"^[a-zA-Z0-9\-\_\.]{1,64}$"

# Common LLM/user alias corrections for DeFiLlama protocol slugs.
_SLUG_ALIASES: dict[str, str] = {
    "spark-protocol": "spark",
    "compound": "compound-v3",
    "curve": "curve-dex",
    "maker": "makerdao",
}


def _normalize_protocol_slug(raw: str) -> str:
    slug = raw.strip().lower()
    if not slug:
        raise ValueError("protocol slug cannot be empty")

    aliased = _SLUG_ALIASES.get(slug)
    if aliased:
        logger.info("DeFiLlama slug alias applied: %s -> %s", slug, aliased)
        slug = aliased

    if not re.match(_SLUG_PATTERN, slug):
        raise ValueError(
            "protocol must be a valid DeFiLlama slug: lowercase letters, "
            "digits, hyphens, underscores, or dots; max 64 characters."
        )
    return slug


# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetProtocolTvlInput(BaseModel):
    protocol: str | None = Field(
        default=None,
        description=(
            "DeFiLlama protocol slug (e.g. 'aave-v3', 'uniswap-v3', 'curve-dex'). "
            "Use lowercase with hyphens as shown on DeFiLlama. "
            "Tip: 'aave' works for the combined Aave TVL; 'aave-v3' for V3 only. "
            "Optional when using batch mode via protocols=[...]."
        ),
    )
    protocols: list[str] = Field(
        default_factory=list,
        description=(
            "Optional batch mode: list of DeFiLlama protocol slugs. "
            "When provided, the tool returns one TVL result object per protocol."
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
    def _validate_slug(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _normalize_protocol_slug(v)

    @field_validator("protocols")
    @classmethod
    def _validate_protocols(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            normalized.append(_normalize_protocol_slug(value))
        return normalized

    @model_validator(mode="after")
    def _require_protocol_or_protocols(self) -> "GetProtocolTvlInput":
        if not self.protocol and not self.protocols:
            raise ValueError("Either protocol or protocols must be provided")
        return self


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
    error: str | None = None
    error_type: str | None = None


# ─── HTTP helpers ─────────────────────────────────────────────────────────────


async def _fetch_current_tvl(slug: str) -> tuple[float | None, str | None, str | None]:
    """Call GET /tvl/{slug} — returns (value, error_type, error_message)."""
    url = f"{_DEFILLAMA_BASE_URL}/tvl/{slug}"
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            session = await get_defillama_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    return float(text.strip()), None, None
                if resp.status == 404:
                    logger.warning("DeFiLlama: protocol not found: %s", slug)
                    return None, "not_found", "Protocol not found on DeFiLlama"
                logger.warning("DeFiLlama /tvl returned status %d for %s", resp.status, slug)
                return None, "upstream_error", f"DeFiLlama /tvl returned HTTP {resp.status}"
        except (ValueError, TypeError):
            logger.warning("DeFiLlama /tvl returned non-numeric response for %s", slug)
            return None, "upstream_error", "DeFiLlama /tvl returned a non-numeric response"
        except asyncio.TimeoutError:
            logger.warning("DeFiLlama /tvl request timed out for %s", slug)
            return None, "timeout", "DeFiLlama /tvl request timed out"
        except (aiohttp.ClientPayloadError, aiohttp.ServerDisconnectedError) as exc:
            if attempt >= _RETRY_ATTEMPTS:
                logger.warning("DeFiLlama /tvl request failed for %s: %s", slug, type(exc).__name__)
                return None, "upstream_error", f"DeFiLlama /tvl request failed: {type(exc).__name__}"
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
        except Exception as exc:
            logger.warning("DeFiLlama /tvl request failed for %s: %s", slug, type(exc).__name__)
            return None, "upstream_error", f"DeFiLlama /tvl request failed: {type(exc).__name__}"
    return None, "upstream_error", "DeFiLlama /tvl request failed"


async def _fetch_protocol_detail(slug: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Call GET /protocol/{slug} — returns (payload, error_type, error_message)."""
    url = f"{_DEFILLAMA_BASE_URL}/protocol/{slug}"
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            session = await get_defillama_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=_PROTOCOL_DETAIL_TIMEOUT_SECONDS)) as resp:
                if resp.status == 200:
                    return await resp.json(), None, None
                if resp.status == 404:
                    logger.warning("DeFiLlama: protocol detail not found: %s", slug)
                    return None, "not_found", "Protocol detail not found on DeFiLlama"
                logger.warning("DeFiLlama /protocol returned status %d for %s", resp.status, slug)
                return None, "upstream_error", f"DeFiLlama /protocol returned HTTP {resp.status}"
        except asyncio.TimeoutError:
            logger.warning("DeFiLlama /protocol request timed out for %s", slug)
            return None, "timeout", "DeFiLlama /protocol request timed out"
        except (aiohttp.ClientPayloadError, aiohttp.ServerDisconnectedError) as exc:
            if attempt >= _RETRY_ATTEMPTS:
                logger.warning("DeFiLlama /protocol request failed for %s: %s", slug, type(exc).__name__)
                return None, "upstream_error", f"DeFiLlama /protocol request failed: {type(exc).__name__}"
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
        except Exception as exc:
            logger.warning("DeFiLlama /protocol request failed for %s: %s", slug, type(exc).__name__)
            return None, "upstream_error", f"DeFiLlama /protocol request failed: {type(exc).__name__}"
    return None, "upstream_error", "DeFiLlama /protocol request failed"


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


def _error_result(
    *,
    protocol: str,
    note: str,
    error_type: str,
    error: str,
) -> dict[str, Any]:
    return GetProtocolTvlOutput(
        protocol=protocol,
        current_tvl_usd=None,
        tvl_7d_change_pct=None,
        tvl_30d_change_pct=None,
        chain_breakdown=[],
        historical_series=None,
        note=note,
        error=error,
        error_type=error_type,
    ).model_dump()


async def _get_protocol_tvl_single(
    protocol: str | None = None,
    include_historical: bool = False,
    days: int = 30,
) -> dict[str, Any]:
    if not protocol or not protocol.strip():
        raise ValueError("protocol is required when protocols is empty")

    slug = _normalize_protocol_slug(protocol)
    cache_key = f"{slug}:{'hist' if include_historical else 'tvl'}:{days}"
    now = time.monotonic()

    cached = _tvl_cache.get(cache_key)
    if cached and now < cached[0]:
        return cached[1]

    if not include_historical:
        # Fast path: single lightweight endpoint.
        tvl_result = await _fetch_current_tvl(slug)
        if isinstance(tvl_result, tuple):
            current_tvl, tvl_error_type, tvl_error = tvl_result
        else:
            current_tvl = tvl_result
            tvl_error_type = None
            tvl_error = None

        if current_tvl is None:
            error_type = tvl_error_type or "upstream_error"
            error = tvl_error or "Protocol not found or DeFiLlama unavailable."
            result = _error_result(
                protocol=slug,
                note="Protocol not found or DeFiLlama unavailable.",
                error_type=error_type,
                error=error,
            )
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
        # Detail path: richer endpoint with chains + full history. Fetch /tvl
        # in parallel so detail timeouts do not serialize fallback latency.
        detail_result, fallback_result = await asyncio.gather(
            _fetch_protocol_detail(slug),
            _fetch_current_tvl(slug),
            return_exceptions=True,
        )

        detail_error_type: str | None = None
        detail_error: str | None = None
        fallback_error_type: str | None = None
        fallback_error: str | None = None

        if isinstance(detail_result, tuple):
            detail, detail_error_type, detail_error = detail_result
        else:
            detail = detail_result if isinstance(detail_result, dict) else None

        if isinstance(fallback_result, tuple):
            fallback_current_tvl, fallback_error_type, fallback_error = fallback_result
        elif isinstance(fallback_result, (int, float)):
            fallback_current_tvl = float(fallback_result)
        else:
            fallback_current_tvl = None

        if isinstance(detail_result, Exception):
            logger.warning("DeFiLlama /protocol request failed for %s: %s", slug, type(detail_result).__name__)
            detail = None
            detail_error_type = "upstream_error"
            detail_error = f"DeFiLlama /protocol request failed: {type(detail_result).__name__}"
        if isinstance(fallback_result, Exception):
            logger.warning("DeFiLlama /tvl request failed for %s: %s", slug, type(fallback_result).__name__)
            fallback_current_tvl = None
            fallback_error_type = "upstream_error"
            fallback_error = f"DeFiLlama /tvl request failed: {type(fallback_result).__name__}"

        if detail is None:
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
                    error=detail_error,
                    error_type=detail_error_type,
                ).model_dump()
                _tvl_cache[cache_key] = (now + 60, result)
                return result

            error_type = detail_error_type or fallback_error_type or "upstream_error"
            error = detail_error or fallback_error or "Protocol not found or DeFiLlama unavailable."
            result = _error_result(
                protocol=slug,
                note="Protocol not found or DeFiLlama unavailable.",
                error_type=error_type,
                error=error,
            )
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


async def get_protocol_tvl(
    protocol: str | None = None,
    protocols: list[str] | None = None,
    include_historical: bool = False,
    days: int = 30,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Get TVL data for a DeFi protocol from DeFiLlama."""
    if protocols:
        # Preserve order while deduplicating to avoid duplicate upstream calls.
        normalized: list[str] = []
        for value in protocols:
            if str(value).strip():
                normalized.append(_normalize_protocol_slug(str(value)))
        batch_slugs = list(dict.fromkeys(normalized))
        if not batch_slugs:
            raise ValueError("protocols must include at least one non-empty slug")
        tasks = [
            asyncio.create_task(
                _get_protocol_tvl_single(
                    protocol=slug,
                    include_historical=include_historical,
                    days=days,
                )
            )
            for slug in batch_slugs
        ]
        index_by_task = {task: idx for idx, task in enumerate(tasks)}
        done, pending = await asyncio.wait(tasks, timeout=_BATCH_TIMEOUT_SECONDS)

        results: dict[int, dict[str, Any] | Exception] = {}
        for task in done:
            idx = index_by_task[task]
            try:
                results[idx] = task.result()
            except Exception as exc:
                results[idx] = exc

        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        output: list[dict[str, Any]] = []
        for idx, slug in enumerate(batch_slugs):
            value = results.get(idx)
            if isinstance(value, dict):
                output.append(value)
                continue
            if isinstance(value, Exception):
                output.append(
                    _error_result(
                        protocol=slug,
                        note="Protocol TVL lookup failed.",
                        error_type="upstream_error",
                        error=f"Protocol TVL lookup failed: {type(value).__name__}",
                    )
                )
                continue

            output.append(
                _error_result(
                    protocol=slug,
                    note="Protocol TVL lookup timed out in batch mode.",
                    error_type="batch_timeout",
                    error="Batch timeout exceeded while waiting for protocol TVL result.",
                )
            )

        return output

    return await _get_protocol_tvl_single(
        protocol=protocol,
        include_historical=include_historical,
        days=days,
    )


# ─── Tool definition ──────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_protocol_tvl",
    version="1.1.0",
    description=(
        "Get Total Value Locked (TVL) data for a DeFi protocol from DeFiLlama. "
        "Returns current TVL in USD, 7-day and 30-day percentage change, and a "
        "per-chain breakdown. Set include_historical=True to also retrieve a daily "
        "TVL series for trend analysis. You can also batch multiple protocols via "
        "protocols=[...]. Supports 3,000+ protocols including Aave, "
        "Uniswap, Curve, Compound, Lido, MakerDAO, and more. "
        "Use the DeFiLlama slug format: 'aave-v3', 'uniswap-v3', 'curve-dex'. "
        "Common aliases such as 'spark-protocol' and 'compound' are auto-corrected."
    ),
    tags=["defi", "tvl", "finance", "protocol", "defillama"],
    input_schema=GetProtocolTvlInput,
    output_schema={
        "anyOf": [
            GetProtocolTvlOutput.model_json_schema(),
            {
                "type": "array",
                "items": GetProtocolTvlOutput.model_json_schema(),
                "minItems": 1,
            },
        ]
    },
    implementation=get_protocol_tvl,
)
