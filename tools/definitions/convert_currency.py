# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""convert_currency – fiat and crypto conversion via CoinGecko + free fiat API."""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── In-process TTL cache for fiat rates ──────────────────────────────────────

_fiat_cache: dict[str, float] | None = None
_fiat_cache_expires: float = 0.0
_FIAT_CACHE_TTL = 300  # 5 minutes

# Known crypto symbols → CoinGecko IDs (top assets)
_CRYPTO_IDS: dict[str, str] = {
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
}

# Common fiat currency codes
_FIAT_CODES = {
    "usd", "eur", "gbp", "jpy", "cny", "krw", "cad", "aud", "chf", "hkd",
    "sgd", "sek", "nok", "dkk", "nzd", "zar", "brl", "inr", "mxn", "try",
}


def _is_crypto(symbol: str) -> bool:
    return symbol.lower() in _CRYPTO_IDS


def _is_fiat(symbol: str) -> bool:
    return symbol.lower() in _FIAT_CODES


# ─── Schemas ──────────────────────────────────────────────────────────────────


class ConvertCurrencyInput(BaseModel):
    amount: float = Field(..., gt=0, description="Amount to convert")
    from_currency: str = Field(
        ..., min_length=1, max_length=20, description="Source currency (e.g. 'ETH', 'USD', 'BTC')"
    )
    to_currency: str = Field(
        ..., min_length=1, max_length=20, description="Target currency (e.g. 'USD', 'EUR', 'BTC')"
    )


class ConvertCurrencyOutput(BaseModel):
    amount: float
    from_currency: str
    to_currency: str
    converted_amount: float
    rate: float
    source: str


# ─── Fiat exchange rate fetcher ───────────────────────────────────────────────


async def _get_fiat_rates(base: str = "USD") -> dict[str, float]:
    """Fetch fiat exchange rates with TTL cache."""
    global _fiat_cache, _fiat_cache_expires

    if _fiat_cache is not None and time.monotonic() < _fiat_cache_expires:
        return _fiat_cache

    url = f"https://api.exchangerate.host/latest?base={base.upper()}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rates = data.get("rates", {})
                    if rates:
                        # Normalize keys to lowercase
                        _fiat_cache = {k.lower(): float(v) for k, v in rates.items()}
                        _fiat_cache_expires = time.monotonic() + _FIAT_CACHE_TTL
                        return _fiat_cache
    except Exception as exc:
        logger.warning("Fiat rate fetch failed: %s", exc)

    # Hardcoded USD fallback for core pairs (stale but functional)
    return {
        "usd": 1.0, "eur": 0.92, "gbp": 0.79, "jpy": 149.5, "cny": 7.24,
        "cad": 1.36, "aud": 1.53, "chf": 0.88, "krw": 1320.0, "inr": 83.1,
    }


async def _get_crypto_price_usd(crypto_id: str) -> float | None:
    """Fetch a single crypto asset price in USD from CoinGecko."""
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={crypto_id}&vs_currencies=usd"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get(crypto_id, {}).get("usd")
    except Exception as exc:
        logger.warning("CoinGecko price fetch failed for %s: %s", crypto_id, exc)
    return None


# ─── Implementation ──────────────────────────────────────────────────────────


async def convert_currency(
    amount: float, from_currency: str, to_currency: str
) -> dict[str, Any]:
    """Convert between fiat and crypto currencies."""
    from_sym = from_currency.strip().lower()
    to_sym = to_currency.strip().lower()

    from_is_crypto = _is_crypto(from_sym)
    to_is_crypto = _is_crypto(to_sym)
    from_is_fiat = _is_fiat(from_sym)
    to_is_fiat = _is_fiat(to_sym)

    if not (from_is_crypto or from_is_fiat):
        return {"error": f"Unknown currency: {from_currency}"}
    if not (to_is_crypto or to_is_fiat):
        return {"error": f"Unknown currency: {to_currency}"}

    rate: float

    if from_is_fiat and to_is_fiat:
        # Fiat-to-fiat
        rates = await _get_fiat_rates("USD")
        from_usd = rates.get(from_sym, 1.0)
        to_usd = rates.get(to_sym, 1.0)
        rate = to_usd / from_usd
        source = "exchangerate.host"

    elif from_is_crypto and to_is_fiat:
        # Crypto-to-fiat: get crypto price in USD, then convert to target fiat
        crypto_id = _CRYPTO_IDS[from_sym]
        price_usd = await _get_crypto_price_usd(crypto_id)
        if price_usd is None:
            return {"error": f"Could not fetch price for {from_currency}"}
        if to_sym == "usd":
            rate = price_usd
        else:
            rates = await _get_fiat_rates("USD")
            fiat_rate = rates.get(to_sym, 1.0)
            rate = price_usd * fiat_rate
        source = "coingecko+exchangerate.host"

    elif from_is_fiat and to_is_crypto:
        # Fiat-to-crypto: invert crypto price
        crypto_id = _CRYPTO_IDS[to_sym]
        price_usd = await _get_crypto_price_usd(crypto_id)
        if price_usd is None:
            return {"error": f"Could not fetch price for {to_currency}"}
        if from_sym == "usd":
            rate = 1.0 / price_usd
        else:
            rates = await _get_fiat_rates("USD")
            from_usd_rate = rates.get(from_sym, 1.0)
            rate = 1.0 / (price_usd * from_usd_rate)
        source = "coingecko+exchangerate.host"

    else:
        # Crypto-to-crypto: get both in USD, compute cross rate
        from_id = _CRYPTO_IDS[from_sym]
        to_id = _CRYPTO_IDS[to_sym]
        from_price = await _get_crypto_price_usd(from_id)
        to_price = await _get_crypto_price_usd(to_id)
        if from_price is None or to_price is None:
            return {"error": f"Could not fetch prices for {from_currency}/{to_currency}"}
        rate = from_price / to_price
        source = "coingecko"

    converted = amount * rate

    return {
        "amount": amount,
        "from_currency": from_currency.upper(),
        "to_currency": to_currency.upper(),
        "converted_amount": round(converted, 8),
        "rate": round(rate, 8),
        "source": source,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="convert_currency",
    version="1.0.0",
    description=(
        "Convert between fiat currencies (USD, EUR, GBP, etc.) and crypto assets "
        "(BTC, ETH, USDC, SOL, etc.). Returns the converted amount and exchange rate."
    ),
    tags=["finance", "currency", "crypto", "conversion"],
    input_schema=ConvertCurrencyInput,
    output_schema=ConvertCurrencyOutput,
    implementation=convert_currency,
)
