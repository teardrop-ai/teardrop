# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_yield_rates – DeFi yield pool rates via DeFiLlama."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import aiohttp
from pydantic import BaseModel, Field, field_validator, model_validator

from teardrop.cache import get_redis
from tools._internals._http_session import get_defillama_session
from tools._internals.provenance import DataProvenance, attach_provenance, utc_now_iso
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── DeFiLlama API ────────────────────────────────────────────────────────────
# Free public API; no authentication required.
_DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"

# ─── In-process TTL cache ─────────────────────────────────────────────────────
# Cache the ENTIRE raw pool list under a single key. Filtering is applied on
# read, so different filter combinations within the TTL window share one fetch.
# APY rates update every few hours; 5-minute TTL is appropriate for agent use.
_pools_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_POOLS_CACHE_KEY = "pools:all"
_POOLS_REDIS_KEY = "tool:get_yield_rates:pools:all"
_POOLS_CACHE_TTL = 300  # seconds
_POOLS_CACHE_ERROR_TTL = 60  # seconds for transient fetch failures
_pools_cache_source_fetched_at: dict[str, str | None] = {}
_POOL_KEEP_FIELDS = frozenset(
    {
        "pool",
        "project",
        "symbol",
        "chain",
        "tvlUsd",
        "apy",
        "apyMean7d",
        "apyMean30d",
        "apyBase",
        "apyReward",
        "stablecoin",
        "ilRisk",
    }
)

# Valid slug/chain name patterns — prevent injection into downstream string ops.
_SLUG_PATTERN = r"^[a-zA-Z0-9\-\_\.]{1,64}$"
_CHAIN_PATTERN = r"^[a-zA-Z0-9\- ]{1,32}$"


# ─── Schemas ──────────────────────────────────────────────────────────────────


def _normalize_cached_pools(raw: Any) -> list[dict[str, Any]]:
    """Defensively normalize cached pool payloads to list[dict]."""
    if not isinstance(raw, list):
        return []
    return [pool for pool in raw if isinstance(pool, dict)]


class GetYieldRatesInput(BaseModel):
    protocols: list[str] | None = Field(
        default=None,
        description=(
            "Filter by DeFiLlama project slugs (e.g. ['aave-v3', 'compound-v3']). None or empty list = include all protocols."
        ),
    )
    chain: str | None = Field(
        default=None,
        description=("Filter by chain name (e.g. 'Ethereum', 'Base', 'Arbitrum'). Case-insensitive. None = all chains."),
    )
    min_tvl_usd: float = Field(
        default=1_000_000.0,
        ge=0.0,
        description="Exclude pools with TVL below this threshold (USD). Default $1M filters noise.",
    )
    min_apy: float = Field(
        default=0.0,
        ge=0.0,
        description="Exclude pools with APY below this value (%). Default 0 includes all.",
    )
    max_apy: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Exclude pools with APY above this value (%). Default None = no upper bound. "
            "Use to filter out leveraged/boosted pools (e.g. max_apy=30) so genuine "
            "stablecoin yields surface."
        ),
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=50,
        description="Maximum number of pools to return, sorted by APY descending.",
    )
    symbols_any: list[str] | None = Field(
        default=None,
        description=(
            "Optional symbol filter. If provided, include pools whose symbol field "
            "contains at least one token from this list (case-insensitive). "
            "Use held token symbols from get_wallet_portfolio to focus results."
        ),
    )
    stable_only: bool = Field(
        default=False,
        description=(
            "When true, return only stablecoin pools and rank by 30d mean APY first "
            "to emphasize consistency over short-term spikes."
        ),
    )

    @model_validator(mode="after")
    def _validate_apy_bounds(self) -> "GetYieldRatesInput":
        if self.max_apy is not None and self.min_apy > self.max_apy:
            raise ValueError("min_apy must be <= max_apy when max_apy is set.")
        return self

    @field_validator("protocols")
    @classmethod
    def _validate_protocols(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        import re

        for slug in v:
            if not re.match(_SLUG_PATTERN, slug):
                raise ValueError(
                    f"Invalid protocol slug '{slug}': must contain only letters, digits, "
                    "hyphens, underscores, or dots; max 64 characters."
                )
        return v

    @field_validator("chain")
    @classmethod
    def _validate_chain(cls, v: str | None) -> str | None:
        if v is None:
            return v
        import re

        if not re.match(_CHAIN_PATTERN, v):
            raise ValueError(
                f"Invalid chain name '{v}': must contain only letters, digits, hyphens, or spaces; max 32 characters."
            )
        return v


class YieldPoolEntry(BaseModel):
    pool_id: str
    project: str
    symbol: str
    chain: str
    tvl_usd: float
    apy: float
    apy_mean_7d: float | None
    apy_mean_30d: float | None
    apy_base: float | None
    apy_reward: float | None
    stable: bool
    il_risk: str | None


class GetYieldRatesOutput(BaseModel):
    pools: list[YieldPoolEntry]
    total_matching: int
    filters_applied: dict[str, Any]
    note: str
    provenance: DataProvenance | None = Field(
        default=None,
        description="Source and freshness metadata for the DeFiLlama response.",
    )


# ─── HTTP helper ──────────────────────────────────────────────────────────────


async def _fetch_pools() -> list[dict[str, Any]]:
    """Call GET /pools and return the raw pool list, or [] on failure."""
    try:
        session = await get_defillama_session()
        async with session.get(_DEFILLAMA_POOLS_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                payload: dict[str, Any] = await resp.json()
                data = payload.get("data", [])
                if isinstance(data, list):
                    # Keep only fields consumed downstream to reduce cache footprint.
                    return [{k: pool.get(k) for k in _POOL_KEEP_FIELDS} for pool in data if isinstance(pool, dict)]
                return []
            logger.warning("DeFiLlama /pools returned status %d", resp.status)
    except Exception as exc:
        logger.warning("DeFiLlama /pools request failed: %s", type(exc).__name__)
    return []


# ─── Filter + extraction helpers ─────────────────────────────────────────────


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a value to float, returning default if conversion fails."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_apy(pool: dict[str, Any]) -> float:
    """Return best available APY: spot apy → apyMean30d → 0.0."""
    apy = pool.get("apy")
    if apy is not None:
        try:
            return float(apy)
        except (TypeError, ValueError):
            pass
    return _safe_float(pool.get("apyMean30d"))


def _resolve_mean_30d(pool: dict[str, Any]) -> float | None:
    """Return parsed 30d mean APY when present, else None."""
    raw = pool.get("apyMean30d")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _sort_key_stable(pool: dict[str, Any]) -> float:
    """Consistency-first key for stable pools: 30d mean when available."""
    mean_30d = _resolve_mean_30d(pool)
    if mean_30d is not None:
        return mean_30d
    return _resolve_apy(pool)


_SYMBOL_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _symbol_tokens(symbol: str) -> set[str]:
    """Split a DeFiLlama symbol into its component token names."""
    return {token for token in _SYMBOL_SPLIT_RE.split(symbol.lower()) if token}


def _select_per_symbol(
    pools: list[dict[str, Any]],
    symbol_terms: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Round-robin each symbol's best pools so every requested symbol is represented."""
    term_set = set(symbol_terms)
    by_symbol: dict[str, list[dict[str, Any]]] = {term: [] for term in symbol_terms}
    for pool in pools:
        for term in _symbol_tokens(str(pool.get("symbol", ""))) & term_set:
            by_symbol[term].append(pool)

    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for rank in range(limit):
        progressed = False
        for term in symbol_terms:
            bucket = by_symbol[term]
            if rank >= len(bucket):
                continue
            progressed = True
            pool = bucket[rank]
            if id(pool) in seen:
                continue
            seen.add(id(pool))
            selected.append(pool)
            if len(selected) >= limit:
                return selected
        if not progressed:
            break
    return selected


def _pool_to_entry(pool: dict[str, Any]) -> YieldPoolEntry:
    """Map a raw DeFiLlama pool dict to a YieldPoolEntry."""
    apy_base_raw = pool.get("apyBase")
    apy_reward_raw = pool.get("apyReward")
    il_risk = pool.get("ilRisk")
    apy_mean_7d_raw = pool.get("apyMean7d")
    apy_mean_30d_raw = pool.get("apyMean30d")

    try:
        apy_base: float | None = float(apy_base_raw) if apy_base_raw is not None else None
    except (TypeError, ValueError):
        apy_base = None
    try:
        apy_reward: float | None = float(apy_reward_raw) if apy_reward_raw is not None else None
    except (TypeError, ValueError):
        apy_reward = None
    try:
        apy_mean_7d: float | None = float(apy_mean_7d_raw) if apy_mean_7d_raw is not None else None
    except (TypeError, ValueError):
        apy_mean_7d = None
    try:
        apy_mean_30d: float | None = float(apy_mean_30d_raw) if apy_mean_30d_raw is not None else None
    except (TypeError, ValueError):
        apy_mean_30d = None

    return YieldPoolEntry(
        pool_id=str(pool.get("pool", "")),
        project=str(pool.get("project", "")),
        symbol=str(pool.get("symbol", "")),
        chain=str(pool.get("chain", "")),
        tvl_usd=_safe_float(pool.get("tvlUsd")),
        apy=_resolve_apy(pool),
        apy_mean_7d=apy_mean_7d,
        apy_mean_30d=apy_mean_30d,
        apy_base=apy_base,
        apy_reward=apy_reward,
        stable=bool(pool.get("stablecoin", False)),
        il_risk=str(il_risk) if il_risk is not None else None,
    )


# ─── Main implementation ──────────────────────────────────────────────────────


async def get_yield_rates(
    protocols: list[str] | None = None,
    chain: str | None = None,
    min_tvl_usd: float = 1_000_000.0,
    min_apy: float = 0.0,
    limit: int = 20,
    symbols_any: list[str] | None = None,
    stable_only: bool = False,
    max_apy: float | None = None,
) -> dict[str, Any]:
    """Get DeFi yield pool rates filtered and sorted by APY."""
    now = time.monotonic()
    redis = get_redis()
    raw_pools: list[dict[str, Any]] = []
    cache_hit = False
    source_fetched_at: str | None = None
    cache_ttl_seconds = _POOLS_CACHE_TTL

    # Multi-container cache path: Redis-first.
    if redis is not None:
        try:
            cached = await redis.get(_POOLS_REDIS_KEY)
            if cached:
                decoded = json.loads(cached)
                if isinstance(decoded, dict) and "data" in decoded:
                    raw_pools = _normalize_cached_pools(decoded.get("data"))
                    source_fetched_at = decoded.get("source_fetched_at")
                else:
                    # Keep compatibility with pre-provenance Redis entries.
                    raw_pools = _normalize_cached_pools(decoded)
                cache_hit = bool(raw_pools)
        except Exception:
            logger.warning("Yield rates Redis cache read failed; refreshing source", exc_info=True)

    # Single-container fallback: in-process cache.
    if not raw_pools and redis is None:
        cached = _pools_cache.get(_POOLS_CACHE_KEY)
        if cached and now < cached[0]:
            raw_pools = cached[1]
            source_fetched_at = _pools_cache_source_fetched_at.get(_POOLS_CACHE_KEY)
            cache_hit = bool(raw_pools)

    if not raw_pools:
        try:
            raw_pools = await _fetch_pools()
        except Exception as exc:
            logger.warning("_fetch_pools raised unexpectedly: %s", type(exc).__name__)
            raw_pools = []

        ttl = _POOLS_CACHE_TTL if raw_pools else _POOLS_CACHE_ERROR_TTL
        source_fetched_at = utc_now_iso() if raw_pools else None
        cache_ttl_seconds = ttl
        if redis is not None:
            try:
                await redis.setex(
                    _POOLS_REDIS_KEY,
                    ttl,
                    json.dumps(
                        {"data": raw_pools, "source_fetched_at": source_fetched_at},
                        separators=(",", ":"),
                    ),
                )
            except Exception:
                logger.warning("Yield rates Redis cache write failed", exc_info=True)
        else:
            _pools_cache[_POOLS_CACHE_KEY] = (now + ttl, raw_pools)
            _pools_cache_source_fetched_at[_POOLS_CACHE_KEY] = source_fetched_at

    if not raw_pools:
        result = GetYieldRatesOutput(
            pools=[],
            total_matching=0,
            filters_applied={},
            note="DeFiLlama yield data unavailable.",
        ).model_dump()
        return attach_provenance(
            result,
            "DeFiLlama",
            [_DEFILLAMA_POOLS_URL],
            cache_hit=cache_hit,
            source_fetched_at=source_fetched_at,
            cache_ttl_seconds=cache_ttl_seconds,
        )

    # Apply filters.
    protocol_set: set[str] | None = {p.lower() for p in protocols} if protocols else None
    chain_lower: str | None = chain.lower() if chain else None

    filtered: list[dict[str, Any]] = []
    for pool in raw_pools:
        if protocol_set is not None and str(pool.get("project", "")).lower() not in protocol_set:
            continue
        if chain_lower is not None and str(pool.get("chain", "")).lower() != chain_lower:
            continue
        if _safe_float(pool.get("tvlUsd")) < min_tvl_usd:
            continue
        if _resolve_apy(pool) < min_apy:
            continue
        if max_apy is not None and _resolve_apy(pool) > max_apy:
            continue
        filtered.append(pool)

    symbol_terms: list[str] = []
    if symbols_any:
        symbol_terms = [s.strip().lower() for s in symbols_any if isinstance(s, str) and s.strip()]
    if symbol_terms:
        term_set = set(symbol_terms)
        filtered = [pool for pool in filtered if _symbol_tokens(str(pool.get("symbol", ""))) & term_set]

    if stable_only:
        filtered = [pool for pool in filtered if bool(pool.get("stablecoin", False))]

    total_matching = len(filtered)

    sort_key = _sort_key_stable if stable_only else _resolve_apy
    filtered.sort(key=sort_key, reverse=True)

    coverage_note = ""
    if symbol_terms:
        matched_terms: set[str] = set()
        for pool in filtered:
            matched_terms |= _symbol_tokens(str(pool.get("symbol", ""))) & set(symbol_terms)
        filtered = _select_per_symbol(filtered, symbol_terms, limit)
        filtered.sort(key=sort_key, reverse=True)
        missing = [term.upper() for term in symbol_terms if term not in matched_terms]
        coverage_note = (
            " Results include the highest-ranked pools for each requested symbol, so this is a complete "
            "per-symbol view rather than a truncated list."
        )
        if missing:
            coverage_note += f" No pools matched these symbols under the current filters: {', '.join(missing)}."
    else:
        filtered = filtered[:limit]

    pools = [_pool_to_entry(p) for p in filtered]

    filters_applied: dict[str, Any] = {
        "protocols": protocols,
        "chain": chain,
        "min_tvl_usd": min_tvl_usd,
        "min_apy": min_apy,
        "max_apy": max_apy,
        "limit": limit,
        "symbols_any": symbols_any,
        "stable_only": stable_only,
    }

    result = GetYieldRatesOutput(
        pools=pools,
        total_matching=total_matching,
        filters_applied=filters_applied,
        note=(
            "Yield data sourced from DeFiLlama. APY values use spot rate; "
            "apyMean30d used as fallback when spot is unavailable. "
            "For consistency-focused analysis, compare apy_mean_30d and apy_reward "
            "instead of relying on short windows alone. "
            "TVL and APY can change rapidly — verify before transacting." + coverage_note
        ),
    ).model_dump()
    return attach_provenance(
        result,
        "DeFiLlama",
        [_DEFILLAMA_POOLS_URL],
        cache_hit=cache_hit,
        source_fetched_at=source_fetched_at,
        cache_ttl_seconds=cache_ttl_seconds,
    )


# ─── Tool definition ──────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_yield_rates",
    version="1.3.0",
    description=(
        "Get DeFi yield pool rates from DeFiLlama, covering 1,000+ protocols across "
        "all chains. Returns pools sorted by APY with TVL, base rate, reward APY, "
        "and 7d/30d mean APY context. "
        "Filter by protocol (e.g. 'aave-v3', 'compound-v3'), chain (e.g. 'Ethereum', "
        "'Base'), minimum TVL, and minimum APY. Use this to answer questions like "
        "'Where can I get the best USDC yield?', 'What is Aave's current APY on Ethereum?', "
        "or 'Compare Aave vs Compound yields'. Returns up to 50 pools. "
        "Call once per query unless a genuinely disjoint filter is required. The returned `symbol` field contains the "
        "underlying tokens (e.g. 'USDC', 'ETH-USDC', 'WBTC'); filter on the client side "
        "by inspecting `symbol` rather than re-calling with different arguments. "
        "Use `min_apy`, `max_apy`, `min_tvl_usd`, and `symbols_any` to prune noise in a single call. "
        "`symbols_any` matches whole symbol tokens (e.g. 'USDC' matches 'USDC' and 'ETH-USDC' but not "
        "'TULIPAUSDC') and returns the highest-ranked pools for EACH requested symbol, so one call gives "
        "a complete per-symbol comparison; symbols with no matching pool are named in `note`. "
        "Set `max_apy` (e.g. 30) to exclude leveraged/boosted pools so genuine yields surface. "
        "Set stable_only=true when you need consistent stablecoin yield screening; this "
        "ranks by 30d mean APY first and still returns spot/base/reward components."
    ),
    tags=["defi", "yield", "apy", "finance", "defillama"],
    input_schema=GetYieldRatesInput,
    output_schema=GetYieldRatesOutput,
    implementation=get_yield_rates,
)
