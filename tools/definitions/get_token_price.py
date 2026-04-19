# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_token_price – crypto asset price lookup via CoinGecko."""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

from config import get_settings
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── In-process TTL cache (per-token) ───────────────────────────────────────
# Keyed by "{cg_id}:{vs_currency}"; value is (expires_at, raw_coingecko_entry).
# Per-token storage means a cached ETH price is reused whether the caller
# requests [ETH] or [BTC, ETH] — drastically reduces upstream API calls.

_token_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_PRICE_CACHE_TTL = 120  # seconds

# Well-known symbol → CoinGecko ID mappings
_SYMBOL_TO_ID: dict[str, str] = {
    "btc": "bitcoin",
    "bitcoin": "bitcoin",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "usdc": "usd-coin",
    "usdt": "tether",
    "dai": "dai",
    "wbtc": "wrapped-bitcoin",
    "weth": "weth",
    "sol": "solana",
    "matic": "matic-network",
    "pol": "matic-network",
    "avax": "avalanche-2",
    "bnb": "binancecoin",
    "link": "chainlink",
    "uni": "uniswap",
    "aave": "aave",
    "arb": "arbitrum",
    "op": "optimism",
    "doge": "dogecoin",
    "xrp": "ripple",
    "ada": "cardano",
    "dot": "polkadot",
    "atom": "cosmos",
    "near": "near",
    "apt": "aptos",
    "sui": "sui",
    "pepe": "pepe",
    "shib": "shiba-inu",
}


def _resolve_id(token: str) -> str:
    """Resolve a token symbol or name to a CoinGecko ID."""
    normalized = token.strip().lower()
    return _SYMBOL_TO_ID.get(normalized, normalized)


# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetTokenPriceInput(BaseModel):
    tokens: list[str] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Token symbols or CoinGecko IDs (e.g. ['BTC', 'ETH', 'SOL'])",
    )
    vs_currency: str = Field(
        default="usd", description="Quote currency (usd, eur, gbp, btc, eth)"
    )


class TokenPriceEntry(BaseModel):
    id: str
    symbol: str
    price: float | None
    market_cap: float | None
    volume_24h: float | None
    change_24h_pct: float | None


class GetTokenPriceOutput(BaseModel):
    vs_currency: str
    prices: list[TokenPriceEntry]


# ─── Implementation ──────────────────────────────────────────────────────────


async def _fetch_from_coingecko(
    ids: list[str], vs: str
) -> dict[str, dict[str, Any]]:
    """Call CoinGecko for the given IDs. Returns raw per-ID data dict."""
    url = (
        f"https://api.coingecko.com/api/v3/simple/price"
        f"?ids={','.join(ids)}&vs_currencies={vs}"
        f"&include_market_cap=true&include_24hr_vol=true&include_24hr_change=true"
    )
    headers: dict[str, str] = {}
    try:
        api_key = get_settings().coingecko_api_key
        if api_key:
            headers["x-cg-demo-api-key"] = api_key
    except Exception:
        pass

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning("CoinGecko returned status %d", resp.status)
    except Exception as exc:
        logger.warning("CoinGecko request failed: %s", exc)
    return {}


async def get_token_price(
    tokens: list[str], vs_currency: str = "usd"
) -> dict[str, Any]:
    """Get current prices for one or more crypto tokens."""
    vs = vs_currency.strip().lower()
    ids = [_resolve_id(t) for t in tokens]
    now = time.monotonic()

    # Split into cached vs uncached — only fetch what we don't already have
    cached: dict[str, dict[str, Any]] = {}
    missing_ids: list[str] = []

    for cg_id in ids:
        entry = _token_cache.get(f"{cg_id}:{vs}")
        if entry and now < entry[0]:
            cached[cg_id] = entry[1]
        else:
            missing_ids.append(cg_id)

    if missing_ids:
        fetched = await _fetch_from_coingecko(missing_ids, vs)
        expires_at = now + _PRICE_CACHE_TTL
        for cg_id in missing_ids:
            data = fetched.get(cg_id, {})
            _token_cache[f"{cg_id}:{vs}"] = (expires_at, data)
            cached[cg_id] = data

    prices = [
        {
            "id": cg_id,
            "symbol": original.upper(),
            "price": cached.get(cg_id, {}).get(vs),
            "market_cap": cached.get(cg_id, {}).get(f"{vs}_market_cap"),
            "volume_24h": cached.get(cg_id, {}).get(f"{vs}_24h_vol"),
            "change_24h_pct": cached.get(cg_id, {}).get(f"{vs}_24h_change"),
        }
        for original, cg_id in zip(tokens, ids)
    ]

    return {"vs_currency": vs, "prices": prices}


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_token_price",
    version="1.0.0",
    description=(
        "Get current price, 24h change, market cap, and volume for one or more "
        "crypto tokens. Accepts symbols (BTC, ETH, SOL) or CoinGecko IDs. "
        "Supports batch queries up to 50 tokens."
    ),
    tags=["finance", "crypto", "price", "market"],
    input_schema=GetTokenPriceInput,
    output_schema=GetTokenPriceOutput,
    implementation=get_token_price,
)
