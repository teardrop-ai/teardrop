# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""get_token_price – crypto asset price lookup via CoinGecko."""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── In-process TTL cache ─────────────────────────────────────────────────────

_price_cache: dict[str, dict[str, Any]] = {}
_price_cache_expires: float = 0.0
_PRICE_CACHE_TTL = 60  # seconds

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


async def get_token_price(
    tokens: list[str], vs_currency: str = "usd"
) -> dict[str, Any]:
    """Get current prices for one or more crypto tokens."""
    global _price_cache, _price_cache_expires

    vs = vs_currency.strip().lower()
    ids = [_resolve_id(t) for t in tokens]
    ids_str = ",".join(ids)
    cache_key = f"{ids_str}:{vs}"

    # Check cache
    if time.monotonic() < _price_cache_expires and cache_key in _price_cache:
        return _price_cache[cache_key]

    url = (
        f"https://api.coingecko.com/api/v3/simple/price"
        f"?ids={ids_str}&vs_currencies={vs}"
        f"&include_market_cap=true&include_24hr_vol=true&include_24hr_change=true"
    )

    # Optionally use API key if configured
    headers: dict[str, str] = {}
    try:
        from config import get_settings
        api_key = get_settings().coingecko_api_key
        if api_key:
            headers["x-cg-demo-api-key"] = api_key
    except Exception:
        pass

    data: dict[str, Any] = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                else:
                    logger.warning("CoinGecko returned status %d", resp.status)
    except Exception as exc:
        logger.warning("CoinGecko request failed: %s", exc)

    prices = []
    for original, cg_id in zip(tokens, ids):
        entry = data.get(cg_id, {})
        prices.append({
            "id": cg_id,
            "symbol": original.upper(),
            "price": entry.get(vs),
            "market_cap": entry.get(f"{vs}_market_cap"),
            "volume_24h": entry.get(f"{vs}_24h_vol"),
            "change_24h_pct": entry.get(f"{vs}_24h_change"),
        })

    result = {"vs_currency": vs, "prices": prices}

    # Update cache
    _price_cache[cache_key] = result
    _price_cache_expires = time.monotonic() + _PRICE_CACHE_TTL

    return result


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
