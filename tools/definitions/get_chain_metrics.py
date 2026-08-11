from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from typing import Any
from urllib.parse import quote

import aiohttp
from pydantic import BaseModel, Field, field_validator

from tools._internals._http_session import get_defillama_session
from tools._internals.provenance import DataProvenance, attach_provenance, utc_now_iso
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

_DEFILLAMA_BASE_URL = "https://api.llama.fi"
_CHAIN_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 ._\-]{0,63}$")
_CACHE_TTL_SECONDS = 300
_ERROR_CACHE_TTL_SECONDS = 60
_CACHE_MAX_ENTRIES = 256
_REQUEST_TIMEOUT_SECONDS = 8
_BATCH_TIMEOUT_SECONDS = 25
_MAX_RESULTS = 50
_MAX_CONCURRENT_SUPPLEMENTS = 5
_MAX_HISTORY_POINTS = 366
_RETRY_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.25

_result_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_chains_cache: tuple[float, list[dict[str, Any]]] | None = None
_chains_cache_source_fetched_at: str | None = None
_chain_history_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_chain_history_cache_source_fetched_at: dict[str, str | None] = {}
_chain_fees_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_chain_fees_cache_source_fetched_at: dict[str, str | None] = {}


def _store_cached(cache: dict[str, tuple[float, Any]], key: str, expires_at: float, value: Any, now: float) -> None:
    for expired_key in [cached_key for cached_key, item in cache.items() if now >= item[0]]:
        cache.pop(expired_key, None)
    if key not in cache and len(cache) >= _CACHE_MAX_ENTRIES:
        cache.pop(min(cache, key=lambda cached_key: cache[cached_key][0]))
    cache[key] = (expires_at, value)


def _normalize_chain_name(value: str) -> str:
    normalized = value.strip()
    if not normalized or not _CHAIN_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "chain names must contain only letters, digits, spaces, periods, underscores, or hyphens; max 64 characters"
        )
    return normalized


class GetChainMetricsInput(BaseModel):
    chains: list[str] | None = Field(
        default=None,
        max_length=_MAX_RESULTS,
        description=(
            "Optional chain names to compare, such as ['Ethereum', 'Arbitrum', 'Solana']. "
            "When omitted, return the highest-TVL chains up to limit."
        ),
    )
    days: int = Field(
        default=30,
        ge=7,
        le=365,
        description="Lookback window used when selecting the historical TVL points to inspect.",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=_MAX_RESULTS,
        description="Maximum number of chain rows to return when the result is not explicitly filtered.",
    )

    @field_validator("chains")
    @classmethod
    def _validate_chains(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            chain = _normalize_chain_name(value)
            key = chain.casefold()
            if key not in seen:
                normalized.append(chain)
                seen.add(key)
        return normalized


class ChainMetricsEntry(BaseModel):
    chain: str
    chain_id: int | str | None
    token_symbol: str | None
    tvl_usd: float | None
    tvl_7d_change_pct: float | None
    tvl_30d_change_pct: float | None
    fees_24h_usd: float | None
    fees_7d_usd: float | None
    fees_30d_usd: float | None
    fees_7d_change_pct: float | None
    fees_30d_change_pct: float | None
    error: str | None = None
    error_type: str | None = None


class GetChainMetricsOutput(BaseModel):
    chains: list[ChainMetricsEntry]
    requested_chains: list[str] | None
    total_available: int
    note: str
    error: str | None = None
    error_type: str | None = None
    provenance: DataProvenance | None = Field(
        default=None,
        description="Source and freshness metadata for the DeFiLlama response.",
    )


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


async def _fetch_json(url: str, resource: str) -> tuple[Any | None, str | None, str | None]:
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            session = await get_defillama_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)) as response:
                if response.status == 200:
                    try:
                        return await response.json(content_type=None), None, None
                    except (TypeError, ValueError):
                        return None, "malformed_response", f"DeFiLlama {resource} returned malformed JSON"
                if response.status == 404:
                    return None, "not_found", f"DeFiLlama {resource} did not find the requested chain"
                return None, "upstream_error", f"DeFiLlama {resource} returned HTTP {response.status}"
        except asyncio.TimeoutError:
            if attempt >= _RETRY_ATTEMPTS:
                return None, "timeout", f"DeFiLlama {resource} request timed out"
        except aiohttp.ClientError as exc:
            if attempt >= _RETRY_ATTEMPTS:
                return None, "upstream_error", f"DeFiLlama {resource} request failed: {type(exc).__name__}"
        except Exception as exc:
            logger.warning("DeFiLlama %s request failed: %s", resource, type(exc).__name__)
            return None, "upstream_error", f"DeFiLlama {resource} request failed: {type(exc).__name__}"

        await asyncio.sleep(_RETRY_BACKOFF_SECONDS)

    return None, "upstream_error", f"DeFiLlama {resource} request failed"


async def _fetch_chains() -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
    payload, error_type, error = await _fetch_json(f"{_DEFILLAMA_BASE_URL}/chains", "/chains")
    if error_type:
        return None, error_type, error
    if not isinstance(payload, list):
        return None, "malformed_response", "DeFiLlama /chains returned a non-list payload"
    return [item for item in payload if isinstance(item, dict)], None, None


async def _fetch_chain_history(chain: str) -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
    encoded_chain = quote(chain, safe="")
    payload, error_type, error = await _fetch_json(
        f"{_DEFILLAMA_BASE_URL}/v2/historicalChainTvl/{encoded_chain}",
        "/v2/historicalChainTvl",
    )
    if error_type:
        return None, error_type, error
    if not isinstance(payload, list):
        return None, "malformed_response", "DeFiLlama historical chain TVL returned a non-list payload"
    return [item for item in payload if isinstance(item, dict)], None, None


async def _fetch_chain_fees(chain: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
    encoded_chain = quote(chain, safe="")
    payload, error_type, error = await _fetch_json(
        f"{_DEFILLAMA_BASE_URL}/overview/fees/{encoded_chain}",
        "/overview/fees",
    )
    if error_type:
        return None, error_type, error
    if not isinstance(payload, dict):
        return None, "malformed_response", "DeFiLlama chain fees returned a non-object payload"
    return payload, None, None


def _normalize_history(raw_history: list[dict[str, Any]] | None, days: int) -> list[tuple[int, float]]:
    if not raw_history:
        return []

    by_timestamp: dict[int, float] = {}
    for item in raw_history:
        try:
            timestamp = int(item.get("date"))
        except (TypeError, ValueError):
            continue
        tvl = _safe_float(item.get("tvl"))
        if tvl is not None:
            by_timestamp[timestamp] = tvl

    points = sorted(by_timestamp.items())
    point_limit = min(_MAX_HISTORY_POINTS, max(days, 30) + 1)
    return points[-point_limit:]


def _change_pct(points: list[tuple[int, float]], days: int) -> float | None:
    if len(points) < 2:
        return None
    current_timestamp, current_value = points[-1]
    target_timestamp = current_timestamp - days * 86_400
    previous_points = [point for point in points[:-1] if point[0] <= target_timestamp]
    if not previous_points:
        return None
    previous_value = previous_points[-1][1]
    if previous_value == 0:
        return None
    return (current_value - previous_value) / previous_value * 100.0


def _cached_chains(now: float) -> list[dict[str, Any]] | None:
    if _chains_cache and now < _chains_cache[0]:
        return _chains_cache[1]
    return None


async def _get_chains_snapshot(now: float) -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
    global _chains_cache, _chains_cache_source_fetched_at

    cached = _cached_chains(now)
    if cached is not None:
        return cached, None, None

    payload, error_type, error = await _fetch_chains()
    if payload is not None:
        _chains_cache = (now + _CACHE_TTL_SECONDS, payload)
        _chains_cache_source_fetched_at = utc_now_iso()
    return payload, error_type, error


async def _get_chain_history(chain: str, now: float) -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
    cache_key = chain.casefold()
    cached = _chain_history_cache.get(cache_key)
    if cached and now < cached[0]:
        return cached[1], None, None

    payload, error_type, error = await _fetch_chain_history(chain)
    if payload is not None:
        _store_cached(_chain_history_cache, cache_key, now + _CACHE_TTL_SECONDS, payload, now)
        _chain_history_cache_source_fetched_at[cache_key] = utc_now_iso()
    return payload, error_type, error


async def _get_chain_fees(chain: str, now: float) -> tuple[dict[str, Any] | None, str | None, str | None]:
    cache_key = chain.casefold()
    cached = _chain_fees_cache.get(cache_key)
    if cached and now < cached[0]:
        return cached[1], None, None

    payload, error_type, error = await _fetch_chain_fees(chain)
    if payload is not None:
        _store_cached(_chain_fees_cache, cache_key, now + _CACHE_TTL_SECONDS, payload, now)
        _chain_fees_cache_source_fetched_at[cache_key] = utc_now_iso()
    return payload, error_type, error


async def _fetch_chain_supplement(
    chain: str,
    now: float,
    semaphore: asyncio.Semaphore,
) -> tuple[tuple[list[dict[str, Any]] | None, str | None, str | None], tuple[dict[str, Any] | None, str | None, str | None]]:
    async with semaphore:
        history_result, fees_result = await asyncio.gather(
            _get_chain_history(chain, now),
            _get_chain_fees(chain, now),
            return_exceptions=True,
        )

    if isinstance(history_result, Exception):
        history_result = (None, "upstream_error", f"Chain TVL history failed: {type(history_result).__name__}")
    if isinstance(fees_result, Exception):
        fees_result = (None, "upstream_error", f"Chain fee history failed: {type(fees_result).__name__}")
    return history_result, fees_result


def _empty_entry(chain: str, error: str, error_type: str) -> ChainMetricsEntry:
    return ChainMetricsEntry(
        chain=chain,
        chain_id=None,
        token_symbol=None,
        tvl_usd=None,
        tvl_7d_change_pct=None,
        tvl_30d_change_pct=None,
        fees_24h_usd=None,
        fees_7d_usd=None,
        fees_30d_usd=None,
        fees_7d_change_pct=None,
        fees_30d_change_pct=None,
        error=error,
        error_type=error_type,
    )


def _build_entry(
    snapshot: dict[str, Any],
    history_result: tuple[list[dict[str, Any]] | None, str | None, str | None],
    fees_result: tuple[dict[str, Any] | None, str | None, str | None],
    days: int,
) -> ChainMetricsEntry:
    chain = str(snapshot.get("name", "")).strip()
    history, history_error_type, history_error = history_result
    fees, fees_error_type, fees_error = fees_result
    points = _normalize_history(history, days)

    errors: list[str] = []
    error_types: list[str] = []
    if history_error:
        errors.append(history_error)
        error_types.append(history_error_type or "upstream_error")
    if fees_error:
        errors.append(fees_error)
        error_types.append(fees_error_type or "upstream_error")

    chain_id = snapshot.get("chainId")
    if not isinstance(chain_id, (int, str)):
        chain_id = None
    token_symbol = snapshot.get("tokenSymbol")
    if not isinstance(token_symbol, str) or not token_symbol.strip():
        token_symbol = None

    return ChainMetricsEntry(
        chain=chain,
        chain_id=chain_id,
        token_symbol=token_symbol,
        tvl_usd=_safe_float(snapshot.get("tvl")),
        tvl_7d_change_pct=_change_pct(points, 7),
        tvl_30d_change_pct=_change_pct(points, 30),
        fees_24h_usd=_safe_float(fees.get("total24h")) if fees else None,
        fees_7d_usd=_safe_float(fees.get("total7d")) if fees else None,
        fees_30d_usd=_safe_float(fees.get("total30d")) if fees else None,
        fees_7d_change_pct=_safe_float(fees.get("change_7dover7d", fees.get("change_7d"))) if fees else None,
        fees_30d_change_pct=_safe_float(fees.get("change_30dover30d", fees.get("change_1m"))) if fees else None,
        error="; ".join(errors) if errors else None,
        error_type="partial_data" if errors else None,
    )


def _cache_key(chains: list[str] | None, days: int, limit: int) -> str:
    return json.dumps(
        {"chains": chains, "days": days, "limit": limit},
        separators=(",", ":"),
        sort_keys=True,
    )


def _source_urls(selected: list[dict[str, Any]]) -> list[str]:
    urls = [f"{_DEFILLAMA_BASE_URL}/chains"]
    for item in selected:
        chain = item.get("name")
        if not isinstance(chain, str) or not chain.strip():
            continue
        encoded_chain = quote(chain, safe="")
        urls.extend(
            [
                f"{_DEFILLAMA_BASE_URL}/v2/historicalChainTvl/{encoded_chain}",
                f"{_DEFILLAMA_BASE_URL}/overview/fees/{encoded_chain}",
            ]
        )
    return list(dict.fromkeys(urls))


def _earliest_source_timestamp(timestamps: list[str | None]) -> str | None:
    if not timestamps or any(not isinstance(timestamp, str) for timestamp in timestamps):
        return None
    return min(timestamps)


async def get_chain_metrics(
    chains: list[str] | None = None,
    days: int = 30,
    limit: int = 20,
) -> dict[str, Any]:
    """Compare chain TVL trends and aggregate fee activity from DeFiLlama."""
    validated = GetChainMetricsInput(chains=chains, days=days, limit=limit)
    normalized_chains = validated.chains
    validated_days = validated.days
    validated_limit = validated.limit
    key = _cache_key(normalized_chains, validated_days, validated_limit)
    now = time.monotonic()
    cached = _result_cache.get(key)
    if cached and now < cached[0]:
        return attach_provenance(cached[1], "DeFiLlama", [], cache_hit=True)

    snapshot_cache_hit = _chains_cache is not None and now < _chains_cache[0]
    snapshot, snapshot_error_type, snapshot_error = await _get_chains_snapshot(now)
    if snapshot is None:
        result = GetChainMetricsOutput(
            chains=[],
            requested_chains=normalized_chains,
            total_available=0,
            note="DeFiLlama chain data unavailable.",
            error=snapshot_error or "DeFiLlama /chains request failed",
            error_type=snapshot_error_type or "upstream_error",
        ).model_dump()
        result = attach_provenance(
            result,
            "DeFiLlama",
            [f"{_DEFILLAMA_BASE_URL}/chains"],
            source_fetched_at=None,
            cache_ttl_seconds=_ERROR_CACHE_TTL_SECONDS,
        )
        _store_cached(_result_cache, key, now + _ERROR_CACHE_TTL_SECONDS, result, now)
        return result

    source_timestamps: list[str | None] = [_chains_cache_source_fetched_at]

    snapshots_by_name: dict[str, dict[str, Any]] = {}
    for item in snapshot:
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            snapshots_by_name.setdefault(name.casefold(), item)

    selected: list[dict[str, Any]] = []
    ordered_requested: list[dict[str, Any] | None] = []
    if normalized_chains:
        for requested_chain in normalized_chains:
            item = snapshots_by_name.get(requested_chain.casefold())
            ordered_requested.append(item)
            if item is not None:
                selected.append(item)
    else:
        selected = sorted(
            [item for item in snapshot if isinstance(item.get("name"), str)],
            key=lambda item: _safe_float(item.get("tvl")) or 0.0,
            reverse=True,
        )[:validated_limit]

    supplement_cache_hit = any(
        (cached_history := _chain_history_cache.get(str(item["name"]).casefold())) is not None
        and now < cached_history[0]
        or (cached_fees := _chain_fees_cache.get(str(item["name"]).casefold())) is not None
        and now < cached_fees[0]
        for item in selected
    )
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SUPPLEMENTS)
    task_by_chain = {
        str(item["name"]): asyncio.create_task(_fetch_chain_supplement(str(item["name"]), now, semaphore)) for item in selected
    }
    done: set[asyncio.Task[Any]] = set()
    pending: set[asyncio.Task[Any]] = set()
    if task_by_chain:
        done, pending = await asyncio.wait(task_by_chain.values(), timeout=_BATCH_TIMEOUT_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    supplement_by_chain: dict[str, Any] = {}
    for chain, task in task_by_chain.items():
        if task not in done:
            supplement_by_chain[chain.casefold()] = (
                (None, "batch_timeout", "Chain metrics supplement timed out"),
                (None, "batch_timeout", "Chain metrics supplement timed out"),
            )
            continue
        try:
            supplement_by_chain[chain.casefold()] = task.result()
        except Exception as exc:
            supplement_by_chain[chain.casefold()] = (
                (None, "upstream_error", f"Chain metrics supplement failed: {type(exc).__name__}"),
                (None, "upstream_error", f"Chain metrics supplement failed: {type(exc).__name__}"),
            )

    for item in selected:
        chain_key = str(item["name"]).casefold()
        history_result, fees_result = supplement_by_chain[chain_key]
        if history_result[0] is not None:
            source_timestamps.append(_chain_history_cache_source_fetched_at.get(chain_key))
        if fees_result[0] is not None:
            source_timestamps.append(_chain_fees_cache_source_fetched_at.get(chain_key))

    entries: list[ChainMetricsEntry] = []
    if normalized_chains:
        for requested_chain, item in zip(normalized_chains, ordered_requested, strict=True):
            if item is None:
                entries.append(
                    _empty_entry(
                        requested_chain,
                        "Chain not found in DeFiLlama /chains",
                        "not_found",
                    )
                )
                continue
            chain = str(item["name"])
            history_result, fees_result = supplement_by_chain[chain.casefold()]
            entries.append(_build_entry(item, history_result, fees_result, validated_days))
    else:
        for item in selected:
            chain = str(item["name"])
            history_result, fees_result = supplement_by_chain[chain.casefold()]
            entries.append(_build_entry(item, history_result, fees_result, validated_days))

    result = GetChainMetricsOutput(
        chains=entries,
        requested_chains=normalized_chains,
        total_available=len(snapshots_by_name),
        note=(
            "Current TVL is sourced from DeFiLlama /chains. TVL changes use the latest available "
            "historical chain series; fees use DeFiLlama's aggregate chain fee overview when available. "
            "Explicit chain filters preserve request order; unfiltered results are capped by limit. "
            "Partial upstream gaps are reported per row."
        ),
    ).model_dump()
    result = attach_provenance(
        result,
        "DeFiLlama",
        _source_urls(selected),
        cache_hit=snapshot_cache_hit or supplement_cache_hit,
        source_fetched_at=_earliest_source_timestamp(source_timestamps),
        cache_ttl_seconds=_CACHE_TTL_SECONDS,
    )
    _store_cached(_result_cache, key, now + _CACHE_TTL_SECONDS, result, now)
    return result


TOOL = ToolDefinition(
    name="get_chain_metrics",
    version="1.1.0",
    description=(
        "Compare blockchain ecosystem health using DeFiLlama current TVL, 7-day and 30-day TVL "
        "changes, and aggregate fee activity. Pass chains such as ['Ethereum', 'Arbitrum', 'Solana'] "
        "for a focused comparison, or omit chains to inspect the highest-TVL ecosystems. "
        "Historical and fee fields fail open when DeFiLlama does not cover a chain."
    ),
    tags=["defi", "chains", "tvl", "fees", "finance", "defillama"],
    input_schema=GetChainMetricsInput,
    output_schema=GetChainMetricsOutput,
    implementation=get_chain_metrics,
)
