# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_wallet_portfolio – aggregated token holdings for a wallet address."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from pydantic import BaseModel, Field
from web3 import Web3

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── Well-known tokens per chain ──────────────────────────────────────────────

_TRACKED_TOKENS: dict[int, list[dict[str, str]]] = {
    1: [  # Ethereum mainnet
        {"address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "symbol": "USDC", "cg_id": "usd-coin", "decimals": "6"},
        {"address": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "symbol": "USDT", "cg_id": "tether", "decimals": "6"},
        {"address": "0x6B175474E89094C44Da98b954EedeAC495271d0F", "symbol": "DAI", "cg_id": "dai", "decimals": "18"},
        {"address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "symbol": "WETH", "cg_id": "weth", "decimals": "18"},
        {"address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "symbol": "WBTC", "cg_id": "wrapped-bitcoin", "decimals": "8"},
    ],
    8453: [  # Base
        {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "symbol": "USDC", "cg_id": "usd-coin", "decimals": "6"},
        {"address": "0x4200000000000000000000000000000000000006", "symbol": "WETH", "cg_id": "weth", "decimals": "18"},
        {"address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "symbol": "DAI", "cg_id": "dai", "decimals": "18"},
    ],
}

_ERC20_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
]

# ─── Price cache (shared lightweight TTL cache) ──────────────────────────────

_portfolio_price_cache: dict[str, float] = {}
_portfolio_price_ts: float = 0.0
_PORTFOLIO_PRICE_TTL = 60  # seconds


async def _fetch_prices(cg_ids: list[str]) -> dict[str, float]:
    """Fetch USD prices from CoinGecko, with caching."""
    global _portfolio_price_cache, _portfolio_price_ts

    now = time.monotonic()
    if now < _portfolio_price_ts + _PORTFOLIO_PRICE_TTL and all(
        cid in _portfolio_price_cache for cid in cg_ids
    ):
        return {cid: _portfolio_price_cache[cid] for cid in cg_ids}

    ids_str = ",".join(set(cg_ids))
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids_str}&vs_currencies=usd"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for cid in cg_ids:
                        price = (data.get(cid) or {}).get("usd", 0.0)
                        _portfolio_price_cache[cid] = price
                    _portfolio_price_ts = now
    except Exception as exc:
        logger.warning("CoinGecko price fetch failed: %s", exc)

    return {cid: _portfolio_price_cache.get(cid, 0.0) for cid in cg_ids}


# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetWalletPortfolioInput(BaseModel):
    wallet_address: str = Field(..., description="Wallet address (0x…)")
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")


class PortfolioEntry(BaseModel):
    symbol: str
    token_address: str | None
    balance_formatted: str
    price_usd: float
    value_usd: float


class GetWalletPortfolioOutput(BaseModel):
    wallet_address: str
    chain_id: int
    total_value_usd: float
    holdings: list[PortfolioEntry]


# ─── Implementation ──────────────────────────────────────────────────────────


async def get_wallet_portfolio(
    wallet_address: str,
    chain_id: int = 1,
) -> dict[str, Any]:
    """Return aggregated token holdings with USD values for a wallet."""
    w3 = get_web3(chain_id)
    wallet = Web3.to_checksum_address(wallet_address)
    tokens = _TRACKED_TOKENS.get(chain_id, [])

    # Collect CoinGecko IDs (ETH + tracked tokens)
    cg_ids = ["ethereum"] + [t["cg_id"] for t in tokens]
    prices = await _fetch_prices(cg_ids)

    # Fetch native ETH balance
    eth_balance_wei = await w3.eth.get_balance(wallet)
    eth_balance = float(Web3.from_wei(eth_balance_wei, "ether"))
    eth_price = prices.get("ethereum", 0.0)

    holdings: list[dict[str, Any]] = [
        {
            "symbol": "ETH",
            "token_address": None,
            "balance_formatted": f"{eth_balance:.6f}",
            "price_usd": eth_price,
            "value_usd": round(eth_balance * eth_price, 2),
        }
    ]

    # Fetch ERC-20 balances concurrently
    async def _get_erc20(token_info: dict[str, str]) -> dict[str, Any] | None:
        try:
            addr = Web3.to_checksum_address(token_info["address"])
            contract = w3.eth.contract(address=addr, abi=_ERC20_BALANCE_ABI)
            raw = await contract.functions.balanceOf(wallet).call()
            decimals = int(token_info["decimals"])
            balance = raw / (10**decimals) if decimals > 0 else float(raw)
            if balance == 0:
                return None
            price = prices.get(token_info["cg_id"], 0.0)
            return {
                "symbol": token_info["symbol"],
                "token_address": addr,
                "balance_formatted": f"{balance:.6f}",
                "price_usd": price,
                "value_usd": round(balance * price, 2),
            }
        except Exception as exc:
            logger.debug("Error fetching %s balance: %s", token_info["symbol"], exc)
            return None

    erc20_results = await asyncio.gather(*[_get_erc20(t) for t in tokens])
    for entry in erc20_results:
        if entry is not None:
            holdings.append(entry)

    # Sort by value descending
    holdings.sort(key=lambda h: h["value_usd"], reverse=True)
    total_value = round(sum(h["value_usd"] for h in holdings), 2)

    return {
        "wallet_address": wallet,
        "chain_id": chain_id,
        "total_value_usd": total_value,
        "holdings": holdings[:20],
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_wallet_portfolio",
    version="1.0.0",
    description=(
        "Get aggregated token holdings with USD values for a wallet address. "
        "Returns native ETH plus major ERC-20 balances (USDC, USDT, DAI, WETH, WBTC) "
        "sorted by value. Supports Ethereum mainnet and Base."
    ),
    tags=["web3", "ethereum", "portfolio", "balance", "defi"],
    input_schema=GetWalletPortfolioInput,
    output_schema=GetWalletPortfolioOutput,
    implementation=get_wallet_portfolio,
)
