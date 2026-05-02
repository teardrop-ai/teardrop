# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_wallet_portfolio – aggregated token holdings for a wallet address."""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from pydantic import BaseModel, Field
from web3 import Web3

from config import get_settings
from tools.definitions._http_session import get_coingecko_session
from tools.definitions._multicall3 import multicall3_batch
from tools.definitions._rpc_semaphore import acquire_rpc_semaphore
from tools.definitions._web3_helpers import get_web3, rpc_call
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── Well-known tokens per chain ──────────────────────────────────────────────

_TRACKED_TOKENS: dict[int, list[dict[str, str]]] = {
    1: [  # Ethereum mainnet (15 major tokens covering 80% of typical DeFi wallets)
        {"address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "symbol": "USDC", "cg_id": "usd-coin", "decimals": "6"},  # noqa: E501
        {"address": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "symbol": "USDT", "cg_id": "tether", "decimals": "6"},  # noqa: E501
        {"address": "0x6B175474E89094C44Da98b954EedeAC495271d0F", "symbol": "DAI", "cg_id": "dai", "decimals": "18"},  # noqa: E501
        {"address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "symbol": "WETH", "cg_id": "weth", "decimals": "18"},  # noqa: E501
        {"address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "symbol": "WBTC", "cg_id": "wrapped-bitcoin", "decimals": "8"},  # noqa: E501
        {"address": "0x514910771af9ca656af840dff83e8264ecf986ca", "symbol": "LINK", "cg_id": "chainlink", "decimals": "18"},  # noqa: E501
        {"address": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984", "symbol": "UNI", "cg_id": "uniswap", "decimals": "18"},  # noqa: E501
        {"address": "0x7fc66500c84a76ad7e9c93437e0273038f7e64ee", "symbol": "AAVE", "cg_id": "aave", "decimals": "18"},  # noqa: E501
        {"address": "0xb50721bcf8d731f670fb3934ea0eaf8c9df82955", "symbol": "ARB", "cg_id": "arbitrum", "decimals": "18"},  # noqa: E501
        {"address": "0x4200000000000000000000000000000000000042", "symbol": "OP", "cg_id": "optimism", "decimals": "18"},  # noqa: E501
        {"address": "0x6de3187eefc0691b5ca162b37bbfc60b8bfe65b0", "symbol": "LDO", "cg_id": "lido-dao", "decimals": "18"},  # noqa: E501
        {"address": "0xae7ab96520de3a18e5e111b5eaab095312d7fe84", "symbol": "stETH", "cg_id": "staked-ether", "decimals": "18"},  # noqa: E501
        {"address": "0xd533a949740bb3306d119cc777fa900ba034cd52", "symbol": "CRV", "cg_id": "curve-dao-token", "decimals": "18"},  # noqa: E501
        {"address": "0x6b3595068778dd592e39a122f4f5a5cf09c90fe2", "symbol": "SUSHI", "cg_id": "sushi", "decimals": "18"},  # noqa: E501
        {"address": "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2", "symbol": "MKR", "cg_id": "maker", "decimals": "18"},  # noqa: E501
    ],
    8453: [  # Base (9 major tokens)
        {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "symbol": "USDC", "cg_id": "usd-coin", "decimals": "6"},  # noqa: E501
        {"address": "0x4200000000000000000000000000000000000006", "symbol": "WETH", "cg_id": "weth", "decimals": "18"},  # noqa: E501
        {"address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "symbol": "DAI", "cg_id": "dai", "decimals": "18"},  # noqa: E501
        {
            "address": "0xd4d42f0b6def4ce0383636d504adfc00e50ed41f",
            "symbol": "cbETH",
            "cg_id": "coinbase-wrapped-staked-eth",
            "decimals": "18",
        },  # noqa: E501
        {
            "address": "0x940181a94a02757d5b3642341111d8f88a6d7efa",
            "symbol": "AERO",
            "cg_id": "aerodrome-finance",
            "decimals": "18",
        },  # noqa: E501
        {"address": "0xeb466d67891d27fdf7b3dffefac43a659d5ff4b9", "symbol": "USDbC", "cg_id": "usd-base-coin", "decimals": "6"},  # noqa: E501
        {"address": "0xa25b9ff59076169048ea43d08ad1326fff9b374d", "symbol": "LDO", "cg_id": "lido-dao", "decimals": "18"},  # noqa: E501
        {"address": "0x0b3b3d9f75d81e005c3bd3762360db25d0da8035", "symbol": "USDe", "cg_id": "ethena-usde", "decimals": "18"},  # noqa: E501
        {
            "address": "0x2ae3f1ec7f1f5012cfeab0151158198f0e09e4ff",
            "symbol": "CURVE",
            "cg_id": "curve-dao-token",
            "decimals": "18",
        },  # noqa: E501
    ],
}

# Function selector for balanceOf(address) — keccak256(sig)[0:4].
_BALANCE_OF_SELECTOR: bytes = bytes(Web3.keccak(text="balanceOf(address)"))[:4]

# ─── Price cache (shared lightweight TTL cache) ──────────────────────────────

_portfolio_price_cache: dict[str, float] = {}
_portfolio_price_ts: float = 0.0
_PORTFOLIO_PRICE_TTL = 60  # seconds

# ─── Note on RPC concurrency ──────────────────────────────────────────────────
# RPC call concurrency is managed globally via acquire_rpc_semaphore() to prevent
# org-level saturation across all concurrent agent runs. Public RPC providers
# (Alchemy, Infura, etc.) enforce 5–10 concurrent call limits.


async def _fetch_prices(cg_ids: list[str]) -> dict[str, float]:
    """Fetch USD prices from CoinGecko, with caching."""
    global _portfolio_price_cache, _portfolio_price_ts

    now = time.monotonic()
    if now < _portfolio_price_ts + _PORTFOLIO_PRICE_TTL and all(cid in _portfolio_price_cache for cid in cg_ids):
        return {cid: _portfolio_price_cache[cid] for cid in cg_ids}

    ids_str = ",".join(set(cg_ids))
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids_str}&vs_currencies=usd"

    headers: dict[str, str] = {}
    try:
        api_key = get_settings().coingecko_api_key
        if api_key:
            headers["x-cg-demo-api-key"] = api_key
    except Exception:
        pass

    try:
        session = await get_coingecko_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10), headers=headers) as resp:
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
    fetch_errors: list[str] = Field(default_factory=list)
    note: str = "Only tracked tokens shown. Untracked tokens not included."


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
    fetch_errors: list[str] = []

    # Fetch native ETH balance (protected by global RPC semaphore and retry wrapper)
    async with acquire_rpc_semaphore():
        try:
            eth_balance_wei = await rpc_call(lambda: w3.eth.get_balance(wallet))
        except Exception as exc:
            logger.warning("ETH balance fetch failed for %s: %s", wallet, exc)
            eth_balance_wei = None
            fetch_errors.append("ETH balance unavailable (RPC/Rate-limit error)")
    eth_balance = float(Web3.from_wei(eth_balance_wei, "ether")) if eth_balance_wei is not None else 0.0
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

    # Fetch ERC-20 balances in a single Multicall3 batch (one RPC call for all tokens).
    erc20_calls = [
        (
            Web3.to_checksum_address(t["address"]),
            _BALANCE_OF_SELECTOR + abi_encode(["address"], [wallet]),
        )
        for t in tokens
    ]
    batch_results = await multicall3_batch(w3, erc20_calls)

    for token_info, (success, return_data) in zip(tokens, batch_results):
        if not success or not return_data:
            logger.debug("balanceOf failed for %s", token_info["symbol"])
            continue
        try:
            raw = abi_decode(["uint256"], return_data)[0]
        except Exception as exc:
            logger.debug("balanceOf decode failed for %s: %s", token_info["symbol"], exc)
            continue
        decimals = int(token_info["decimals"])
        balance = raw / (10**decimals) if decimals > 0 else float(raw)
        if balance == 0:
            continue
        price = prices.get(token_info["cg_id"], 0.0)
        holdings.append(
            {
                "symbol": token_info["symbol"],
                "token_address": Web3.to_checksum_address(token_info["address"]),
                "balance_formatted": f"{balance:.6f}",
                "price_usd": price,
                "value_usd": round(balance * price, 2),
            }
        )

    # Sort by value descending
    holdings.sort(key=lambda h: h["value_usd"], reverse=True)
    total_value = round(sum(h["value_usd"] for h in holdings), 2)

    return {
        "wallet_address": wallet,
        "chain_id": chain_id,
        "total_value_usd": total_value,
        "holdings": holdings[:20],
        "fetch_errors": fetch_errors,
        "note": "Only tracked tokens shown. Untracked tokens not included.",
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_wallet_portfolio",
    version="1.0.0",
    description=(
        "Get aggregated token holdings with USD values for a wallet address. "
        "Tracks 15+ major tokens on Ethereum "
        "(USDC, USDT, DAI, WETH, WBTC, LINK, UNI, AAVE, ARB, OP, LDO, stETH, CRV, SUSHI, MKR) "
        "and 9+ on Base. Sorted by USD value. Returns up to 20 holdings. "
        "Includes native ETH balance in the holdings list — calling get_eth_balance "
        "separately after this is redundant."
    ),
    tags=["web3", "ethereum", "portfolio", "balance", "defi"],
    input_schema=GetWalletPortfolioInput,
    output_schema=GetWalletPortfolioOutput,
    implementation=get_wallet_portfolio,
)
