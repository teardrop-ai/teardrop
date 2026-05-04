# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_yield_rates – DeFi yield pool rates via DeFiLlama."""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
from pydantic import BaseModel, Field, field_validator

from tools.registry import ToolDefinition
from tools.definitions._http_session import get_defillama_session

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
_POOLS_CACHE_TTL = 300  # seconds
_POOLS_CACHE_ERROR_TTL = 60  # seconds for transient fetch failures
_POOL_KEEP_FIELDS = frozenset(
    {
        "pool",
        "project",
        "symbol",
        "chain",
        "tvlUsd",
        "apy",
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
    apy_base: float | None
    apy_reward: float | None
    stable: bool
    il_risk: str | None


class GetYieldRatesOutput(BaseModel):
    pools: list[YieldPoolEntry]
    total_matching: int
    filters_applied: dict[str, Any]
    note: str


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
                    return [
                        {k: pool.get(k) for k in _POOL_KEEP_FIELDS}
                        for pool in data
                        if isinstance(pool, dict)
                    ]
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


def _pool_to_entry(pool: dict[str, Any]) -> YieldPoolEntry:
    """Map a raw DeFiLlama pool dict to a YieldPoolEntry."""
    apy_base_raw = pool.get("apyBase")
    apy_reward_raw = pool.get("apyReward")
    il_risk = pool.get("ilRisk")

    try:
        apy_base: float | None = float(apy_base_raw) if apy_base_raw is not None else None
    except (TypeError, ValueError):
        apy_base = None
    try:
        apy_reward: float | None = float(apy_reward_raw) if apy_reward_raw is not None else None
    except (TypeError, ValueError):
        apy_reward = None

    return YieldPoolEntry(
        pool_id=str(pool.get("pool", "")),
        project=str(pool.get("project", "")),
        symbol=str(pool.get("symbol", "")),
        chain=str(pool.get("chain", "")),
        tvl_usd=_safe_float(pool.get("tvlUsd")),
        apy=_resolve_apy(pool),
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
) -> dict[str, Any]:
    """Get DeFi yield pool rates filtered and sorted by APY."""
    now = time.monotonic()

    # Fetch or serve from cache.
    cached = _pools_cache.get(_POOLS_CACHE_KEY)
    if cached and now < cached[0]:
        raw_pools = cached[1]
    else:
        try:
            raw_pools = await _fetch_pools()
        except Exception as exc:
            logger.warning("_fetch_pools raised unexpectedly: %s", type(exc).__name__)
            raw_pools = []
        ttl = _POOLS_CACHE_TTL if raw_pools else _POOLS_CACHE_ERROR_TTL
        _pools_cache[_POOLS_CACHE_KEY] = (now + ttl, raw_pools)

    if not raw_pools:
        return GetYieldRatesOutput(
            pools=[],
            total_matching=0,
            filters_applied={},
            note="DeFiLlama yield data unavailable.",
        ).model_dump()

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
        filtered.append(pool)

    if symbols_any:
        symbol_terms = [s.strip().lower() for s in symbols_any if isinstance(s, str) and s.strip()]
        if symbol_terms:
            filtered = [pool for pool in filtered if any(term in str(pool.get("symbol", "")).lower() for term in symbol_terms)]

    total_matching = len(filtered)

    # Sort by APY descending, then slice.
    filtered.sort(key=_resolve_apy, reverse=True)
    filtered = filtered[:limit]

    pools = [_pool_to_entry(p) for p in filtered]

    filters_applied: dict[str, Any] = {
        "protocols": protocols,
        "chain": chain,
        "min_tvl_usd": min_tvl_usd,
        "min_apy": min_apy,
        "limit": limit,
        "symbols_any": symbols_any,
    }

    return GetYieldRatesOutput(
        pools=pools,
        total_matching=total_matching,
        filters_applied=filters_applied,
        note=(
            "Yield data sourced from DeFiLlama. APY values use spot rate; "
            "apyMean30d used as fallback when spot is unavailable. "
            "TVL and APY can change rapidly — verify before transacting."
        ),
    ).model_dump()


# ─── Tool definition ──────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_yield_rates",
    version="1.0.0",
    description=(
        "Get DeFi yield pool rates from DeFiLlama, covering 1,000+ protocols across "
        "all chains. Returns pools sorted by APY with TVL, base rate, and reward APY. "
        "Filter by protocol (e.g. 'aave-v3', 'compound-v3'), chain (e.g. 'Ethereum', "
        "'Base'), minimum TVL, and minimum APY. Use this to answer questions like "
        "'Where can I get the best USDC yield?', 'What is Aave's current APY on Ethereum?', "
        "or 'Compare Aave vs Compound yields'. Returns up to 50 pools. "
        "IMPORTANT: Call ONCE per query. The returned `symbol` field contains the "
        "underlying tokens (e.g. 'USDC', 'ETH-USDC', 'WBTC'); filter on the client side "
        "by inspecting `symbol` rather than re-calling with different arguments. "
        "Use `min_apy`, `min_tvl_usd`, and `symbols_any` to prune noise in a single call."
    ),
    tags=["defi", "yield", "apy", "finance", "defillama"],
    input_schema=GetYieldRatesInput,
    output_schema=GetYieldRatesOutput,
    implementation=get_yield_rates,
)
