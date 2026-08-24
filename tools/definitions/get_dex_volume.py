# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from typing import Any

import aiohttp
from pydantic import BaseModel, Field, field_validator

from tools._internals._http_session import get_defillama_session
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

_DEFILLAMA_DEX_URL = "https://api.llama.fi/overview/dexs"
_PROTOCOL_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 ._\-]{0,63}$")
_MAX_PROTOCOL_FILTERS = 50
_MAX_LIMIT = 50
_CACHE_TTL_SECONDS = 300
_ERROR_CACHE_TTL_SECONDS = 60
_CACHE_MAX_ENTRIES = 256
_REQUEST_TIMEOUT_SECONDS = 12
_RETRY_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.25

_result_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_overview_cache: tuple[float, dict[str, Any]] | None = None


def _store_cached(cache: dict[str, tuple[float, Any]], key: str, expires_at: float, value: Any, now: float) -> None:
    for expired_key in [cached_key for cached_key, item in cache.items() if now >= item[0]]:
        cache.pop(expired_key, None)
    if key not in cache and len(cache) >= _CACHE_MAX_ENTRIES:
        cache.pop(min(cache, key=lambda cached_key: cache[cached_key][0]))
    cache[key] = (expires_at, value)


def _normalize_protocol(value: str) -> str:
    normalized = value.strip()
    if not normalized or not _PROTOCOL_PATTERN.fullmatch(normalized):
        raise ValueError(
            "protocol filters must contain only letters, digits, spaces, periods, underscores, or hyphens; max 64 characters"
        )
    return normalized


class GetDexVolumeInput(BaseModel):
    protocols: list[str] | None = Field(
        default=None,
        max_length=_MAX_PROTOCOL_FILTERS,
        description=(
            "Optional protocol names or DeFiLlama slugs, such as ['uniswap-v3', 'curve-dex']. "
            "When omitted, return the largest DEX protocols."
        ),
    )
    lookback_days: int = Field(
        default=30,
        ge=1,
        le=30,
        description="Window used to rank the returned protocols: 1, 7, or 30 days (nearest supported window is used).",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=_MAX_LIMIT,
        description="Maximum number of DEX protocols to return.",
    )

    @field_validator("protocols")
    @classmethod
    def _validate_protocols(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            protocol = _normalize_protocol(value)
            key = protocol.casefold()
            if key not in seen:
                normalized.append(protocol)
                seen.add(key)
        return normalized


class DexVolumeEntry(BaseModel):
    protocol: str
    slug: str | None
    category: str | None
    volume_24h_usd: float | None
    volume_7d_usd: float | None
    volume_30d_usd: float | None
    volume_7d_change_pct: float | None
    volume_30d_change_pct: float | None
    volume_share_pct: float | None
    chains: list[str]
    error: str | None = None
    error_type: str | None = None


class GetDexVolumeOutput(BaseModel):
    dexes: list[DexVolumeEntry]
    total_matching: int
    total_available: int
    volume_share_basis: str
    note: str
    error: str | None = None
    error_type: str | None = None


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _first_present(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None:
            return value
    return None


async def _fetch_json(url: str) -> tuple[Any | None, str | None, str | None]:
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            session = await get_defillama_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)) as response:
                if response.status == 200:
                    try:
                        return await response.json(content_type=None), None, None
                    except (TypeError, ValueError):
                        return None, "malformed_response", "DeFiLlama /overview/dexs returned malformed JSON"
                if response.status == 404:
                    return None, "not_found", "DeFiLlama DEX overview was not found"
                return None, "upstream_error", f"DeFiLlama /overview/dexs returned HTTP {response.status}"
        except asyncio.TimeoutError:
            if attempt >= _RETRY_ATTEMPTS:
                return None, "timeout", "DeFiLlama /overview/dexs request timed out"
        except aiohttp.ClientError as exc:
            if attempt >= _RETRY_ATTEMPTS:
                return None, "upstream_error", f"DeFiLlama /overview/dexs request failed: {type(exc).__name__}"
        except Exception as exc:
            logger.warning("DeFiLlama /overview/dexs request failed: %s", type(exc).__name__)
            return None, "upstream_error", f"DeFiLlama /overview/dexs request failed: {type(exc).__name__}"

        await asyncio.sleep(_RETRY_BACKOFF_SECONDS)

    return None, "upstream_error", "DeFiLlama /overview/dexs request failed"


async def _fetch_dex_overview() -> tuple[dict[str, Any] | None, str | None, str | None]:
    payload, error_type, error = await _fetch_json(_DEFILLAMA_DEX_URL)
    if error_type:
        return None, error_type, error
    if not isinstance(payload, dict):
        return None, "malformed_response", "DeFiLlama /overview/dexs returned a non-object payload"
    return payload, None, None


async def _get_dex_overview(now: float) -> tuple[dict[str, Any] | None, str | None, str | None]:
    global _overview_cache
    if _overview_cache and now < _overview_cache[0]:
        return _overview_cache[1], None, None

    payload, error_type, error = await _fetch_dex_overview()
    if payload is not None:
        _overview_cache = (now + _CACHE_TTL_SECONDS, payload)
    return payload, error_type, error


def _protocol_names(record: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("name", "displayName", "slug", "defillamaId", "id"):
        value = record.get(key)
        if value is not None:
            names.add(str(value).casefold())
    return names


def _sort_volume(record: dict[str, Any], lookback_days: int) -> float:
    if lookback_days <= 1:
        value = record.get("total24h")
    elif lookback_days <= 7:
        value = record.get("total7d")
    else:
        value = record.get("total30d")
    return _safe_float(value) or 0.0


def _volume_share_denominator(payload: dict[str, Any], records: list[dict[str, Any]]) -> tuple[float | None, str]:
    total_24h = _safe_float(payload.get("total24h"))
    if total_24h is not None and total_24h > 0:
        return total_24h, "DeFiLlama /overview/dexs total24h across the reported DEX landscape"

    summed_24h = sum(_safe_float(record.get("total24h")) or 0.0 for record in records)
    if summed_24h > 0:
        return summed_24h, "sum of reported DEX protocol total24h values"
    return None, "unavailable because DeFiLlama returned no usable total24h denominator"


def _record_to_entry(record: dict[str, Any], denominator: float | None) -> DexVolumeEntry:
    protocol = str(record.get("displayName") or record.get("name") or record.get("slug") or "Unknown DEX")
    volume_24h = _safe_float(record.get("total24h"))
    volume_7d = _safe_float(record.get("total7d"))
    volume_30d = _safe_float(record.get("total30d"))
    volume_fields = (volume_24h, volume_7d, volume_30d)
    chains = record.get("chains")
    if not isinstance(chains, list):
        chains = []
    normalized_chains = [str(chain) for chain in chains if isinstance(chain, str)][:50]

    error = None
    error_type = None
    if all(value is None for value in volume_fields):
        error = "DEX volume fields unavailable in DeFiLlama response"
        error_type = "malformed_data"

    share = None
    if denominator and volume_24h is not None:
        share = volume_24h / denominator * 100.0

    return DexVolumeEntry(
        protocol=protocol,
        slug=str(record["slug"]) if record.get("slug") is not None else None,
        category=str(record["category"]) if record.get("category") is not None else None,
        volume_24h_usd=volume_24h,
        volume_7d_usd=volume_7d,
        volume_30d_usd=volume_30d,
        volume_7d_change_pct=_safe_float(_first_present(record, ("change_7dover7d", "change_7d"))),
        volume_30d_change_pct=_safe_float(_first_present(record, ("change_30dover30d", "change_1m"))),
        volume_share_pct=share,
        chains=normalized_chains,
        error=error,
        error_type=error_type,
    )


def _cache_key(protocols: list[str] | None, lookback_days: int, limit: int) -> str:
    return json.dumps(
        {"protocols": protocols, "lookback_days": lookback_days, "limit": limit},
        separators=(",", ":"),
        sort_keys=True,
    )


async def get_dex_volume(
    protocols: list[str] | None = None,
    lookback_days: int = 30,
    limit: int = 20,
) -> dict[str, Any]:
    """Return DeFiLlama DEX volumes, changes, and global 24-hour volume shares."""
    validated = GetDexVolumeInput(protocols=protocols, lookback_days=lookback_days, limit=limit)
    key = _cache_key(validated.protocols, validated.lookback_days, validated.limit)
    now = time.monotonic()
    cached = _result_cache.get(key)
    if cached and now < cached[0]:
        return cached[1]

    payload, overview_error_type, overview_error = await _get_dex_overview(now)
    if payload is None:
        result = GetDexVolumeOutput(
            dexes=[],
            total_matching=0,
            total_available=0,
            volume_share_basis="unavailable",
            note="DeFiLlama DEX volume data unavailable.",
            error=overview_error or "DeFiLlama /overview/dexs request failed",
            error_type=overview_error_type or "upstream_error",
        ).model_dump()
        _store_cached(_result_cache, key, now + _ERROR_CACHE_TTL_SECONDS, result, now)
        return result

    raw_records = payload.get("protocols", [])
    records = [record for record in raw_records if isinstance(record, dict)] if isinstance(raw_records, list) else []
    denominator, share_basis = _volume_share_denominator(payload, records)
    filters = {protocol.casefold() for protocol in validated.protocols} if validated.protocols else None
    if filters is not None:
        records = [record for record in records if _protocol_names(record) & filters]

    total_matching = len(records)
    records.sort(key=lambda record: _sort_volume(record, validated.lookback_days), reverse=True)
    selected = records[: validated.limit]

    result = GetDexVolumeOutput(
        dexes=[_record_to_entry(record, denominator) for record in selected],
        total_matching=total_matching,
        total_available=len(payload.get("protocols", [])) if isinstance(payload.get("protocols"), list) else 0,
        volume_share_basis=share_basis,
        note=(
            "Volume is sourced from DeFiLlama /overview/dexs. 24-hour share is calculated against "
            "the unfiltered reported DEX total when available, so filtered results retain landscape context. "
            f"Protocols are ranked by the requested {validated.lookback_days}-day lookback window and capped at "
            f"{validated.limit} rows."
        ),
    ).model_dump()
    _store_cached(_result_cache, key, now + _CACHE_TTL_SECONDS, result, now)
    return result


TOOL = ToolDefinition(
    name="get_dex_volume",
    version="1.0.0",
    description=(
        "Compare decentralized exchange activity from DeFiLlama. Returns 24-hour, 7-day, and 30-day "
        "volume, period-over-period changes, and each protocol's share of reported global 24-hour DEX volume. "
        "Filter by protocol names or DeFiLlama slugs and rank by a 1-, 7-, or 30-day lookback window."
    ),
    tags=["defi", "dex", "volume", "finance", "defillama"],
    input_schema=GetDexVolumeInput,
    output_schema=GetDexVolumeOutput,
    implementation=get_dex_volume,
)
