# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_dex_quote – on-chain Uniswap v3 swap quote via QuoterV2.

Pure-RPC swap quote. Queries all four Uniswap v3 fee tiers in parallel via
``QuoterV2.quoteExactInputSingle`` and returns the best (highest ``amountOut``)
result. No external aggregator APIs — zero dependency beyond the configured
Ethereum / Base RPC endpoint.

Chains: Ethereum mainnet (1) and Base mainnet (8453) only.

Design notes
------------
* QuoterV2 is not ``view``; it executes ``pool.swap`` inside try/catch and
  returns data via revert. web3.py's ``.call()`` handles the revert-decoded
  return values transparently.
* ``sqrtPriceLimitX96 == 0`` is explicitly handled inside the Quoter source
  (substituted with ``TickMath.MIN_SQRT_RATIO + 1`` or ``MAX_SQRT_RATIO - 1``)
  so ``0`` is the correct, safe value for unconstrained quotes.
* Non-existent pools revert with ``'Unexpected error'`` — surfaced as
  ``ContractLogicError`` and recorded as ``error="pool_not_found"`` per tier
  without failing the whole call.
* Handler catches all expected errors and returns structured output; tool
  callers are billed per invocation regardless of handler exceptions
  (see ``agent/nodes.py`` + ``billing.calculate_run_cost_usdc``), so only
  unexpected infrastructure failures should raise.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal, getcontext
from typing import Any

from pydantic import BaseModel, Field, field_validator
from web3 import Web3
from web3.exceptions import ContractLogicError

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# Ample precision for token math; raw uint256 values can reach 10^30+.
getcontext().prec = 60

# ─── Constants ────────────────────────────────────────────────────────────────

# QuoterV2 deployments — primary source: developers.uniswap.org (April 2026).
_QUOTER_V2: dict[int, str] = {
    1: "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    8453: "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
}

# Uniswap v3 fee tiers (hundredths of a bip).
_FEE_TIERS: tuple[int, ...] = (100, 500, 3000, 10000)

# Well-known ERC-20 decimals per chain — avoids an eth_call for common tokens.
# Addresses are EIP-55 checksummed.
_KNOWN_DECIMALS: dict[int, dict[str, int]] = {
    1: {
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2": 18,  # WETH
        "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0": 18,  # wstETH
        "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": 8,  # WBTC
        "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf": 8,  # cbBTC
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": 6,  # USDC
        "0xdAC17F958D2ee523a2206206994597C13D831ec7": 6,  # USDT
        "0x6B175474E89094C44Da98b954EedeAC495271d0F": 18,  # DAI
    },
    8453: {
        "0x4200000000000000000000000000000000000006": 18,  # WETH
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913": 6,  # USDC (native)
        "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA": 6,  # USDbC (bridged)
        "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf": 8,  # cbBTC
        "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb": 18,  # DAI
    },
}

# Concurrency gate: 2 decimals() + 4 tier quotes = 6 eth_calls worst case.
_SEM_LIMIT = 6

# Amount cap: real token supplies don't exceed 2^96; 2^128 is a generous
# safety cap that prevents overflow edge cases while accommodating tokens
# with unusual precision.
_MAX_AMOUNT_IN: int = 2**128

# Decimals TTL cache — decimals are immutable on any legitimate ERC-20, so a
# 24h TTL is purely a safeguard against mistakenly cached values.
_decimals_cache: dict[tuple[int, str], tuple[float, int]] = {}
_DECIMALS_CACHE_TTL = 86400  # 24 hours

# ─── Minimal ABIs ─────────────────────────────────────────────────────────────

_QUOTER_V2_ABI: list[dict[str, Any]] = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

_ERC20_DECIMALS_ABI: list[dict[str, Any]] = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    }
]


# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetDexQuoteInput(BaseModel):
    token_in: str = Field(
        ...,
        description="EIP-55 checksummed address of the token being sold. "
        "For native ETH, pass the WETH address (Ethereum: "
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2; Base: "
        "0x4200000000000000000000000000000000000006).",
    )
    token_out: str = Field(
        ...,
        description="EIP-55 checksummed address of the token being bought.",
    )
    amount_in: str = Field(
        ...,
        description="Input amount in RAW uint256 units (e.g. '1000000' for 1 USDC, "
        "'1000000000000000000' for 1 WETH). Must be > 0 and < 2^128.",
    )
    chain_id: int = Field(
        default=1,
        description="1 = Ethereum mainnet, 8453 = Base mainnet. Other chains unsupported.",
    )

    @field_validator("token_in", "token_out")
    @classmethod
    def _validate_checksum(cls, v: str) -> str:
        if not isinstance(v, str) or not Web3.is_checksum_address(v):
            raise ValueError(f"Address must be EIP-55 checksummed: {v}")
        return v

    @field_validator("amount_in")
    @classmethod
    def _validate_amount(cls, v: str) -> str:
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError("amount_in must be a decimal string of a uint256")
        if n <= 0:
            raise ValueError("amount_in must be > 0")
        if n >= _MAX_AMOUNT_IN:
            raise ValueError("amount_in exceeds safety cap of 2^128")
        return str(n)

    @field_validator("chain_id")
    @classmethod
    def _validate_chain(cls, v: int) -> int:
        if v not in _QUOTER_V2:
            raise ValueError(f"Unsupported chain_id={v}; supported: {sorted(_QUOTER_V2)}")
        return v


class TierQuote(BaseModel):
    fee_tier: int
    success: bool
    amount_out: str | None = None
    sqrt_price_x96_after: str | None = None
    gas_estimate: str | None = None
    error: str | None = None


class GetDexQuoteOutput(BaseModel):
    chain_id: int
    token_in: str
    token_out: str
    amount_in: str
    amount_in_decimals: int
    amount_out: str
    amount_out_decimals: int
    amount_out_human: str
    effective_rate: str
    fee_tier_used: int | None
    quotes_per_tier: list[TierQuote]
    no_liquidity: bool
    block_number: int


# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _resolve_decimals(w3: Any, chain_id: int, address: str, sem: asyncio.Semaphore) -> int:
    """Return the ERC-20 decimals for ``address`` on ``chain_id``.

    Static map → TTL cache → on-chain ``decimals()`` fallback. On failure,
    defaults to 18 (the ERC-20 convention) with a warning; this keeps the
    tool usable for obscure tokens while flagging any anomaly in logs.
    """
    static = _KNOWN_DECIMALS.get(chain_id, {}).get(address)
    if static is not None:
        return static

    key = (chain_id, address)
    cached = _decimals_cache.get(key)
    now = time.monotonic()
    if cached and now < cached[0]:
        return cached[1]

    async with sem:
        try:
            contract = w3.eth.contract(address=address, abi=_ERC20_DECIMALS_ABI)
            value: int = await contract.functions.decimals().call()
        except Exception as exc:
            logger.warning(
                "decimals() fallback to 18 for %s on chain %d: %s",
                address,
                chain_id,
                exc,
            )
            value = 18

    _decimals_cache[key] = (now + _DECIMALS_CACHE_TTL, int(value))
    return int(value)


async def _quote_one_tier(
    quoter: Any,
    token_in: str,
    token_out: str,
    amount_in: int,
    fee: int,
    sem: asyncio.Semaphore,
) -> TierQuote:
    """Run QuoterV2.quoteExactInputSingle for a single fee tier.

    Reverts are caught and mapped to structured ``error`` values; only a
    successful quote returns ``success=True``.
    """
    params = (token_in, token_out, amount_in, fee, 0)
    async with sem:
        try:
            result = await quoter.functions.quoteExactInputSingle(params).call()
        except ContractLogicError as exc:
            msg = str(exc) or "unknown_revert"
            # 'Unexpected error' is the QuoterV2 signal for a non-existent pool.
            err = "pool_not_found" if "Unexpected error" in msg else msg[:160]
            return TierQuote(fee_tier=fee, success=False, error=err)
        except Exception as exc:
            # Distinguish infrastructure failures (network, timeout) by re-logging;
            # still return a structured TierQuote so the parent call can complete
            # with partial data rather than failing the entire quote.
            logger.debug("quoter tier=%d failed: %s", fee, exc)
            return TierQuote(fee_tier=fee, success=False, error=str(exc)[:160])

    amount_out, sqrt_price_x96_after, _ticks, gas_estimate = result
    return TierQuote(
        fee_tier=fee,
        success=True,
        amount_out=str(amount_out),
        sqrt_price_x96_after=str(sqrt_price_x96_after),
        gas_estimate=str(gas_estimate),
    )


def _format_human(raw: int, decimals: int) -> str:
    """Format a raw uint256 token amount as a human decimal string."""
    if raw == 0:
        return "0"
    d = Decimal(raw) / (Decimal(10) ** decimals)
    # normalize() drops trailing zeros; format to avoid scientific notation.
    s = format(d.normalize(), "f")
    # Decimal.normalize() on whole numbers produces "1E+2" style — format("f")
    # already handles it; guard against leading/trailing anomalies.
    return s


# ─── Implementation ──────────────────────────────────────────────────────────


async def get_dex_quote(
    token_in: str,
    token_out: str,
    amount_in: str,
    chain_id: int = 1,
) -> dict[str, Any]:
    """Return the best Uniswap v3 swap quote across all fee tiers.

    All four fee tiers (100 / 500 / 3000 / 10000 bps) are queried in parallel.
    Per-tier reverts are isolated; the tier with the highest ``amountOut`` wins.
    Returns ``no_liquidity=True`` if every tier reverts.
    """
    if token_in == token_out:
        raise ValueError("token_in and token_out must differ")

    w3 = get_web3(chain_id)
    sem = asyncio.Semaphore(_SEM_LIMIT)
    quoter_address = _QUOTER_V2[chain_id]
    quoter = w3.eth.contract(address=quoter_address, abi=_QUOTER_V2_ABI)

    amount_in_int = int(amount_in)

    # Resolve decimals and block number in parallel with the tier quotes.
    decimals_in_task = asyncio.create_task(_resolve_decimals(w3, chain_id, token_in, sem))
    decimals_out_task = asyncio.create_task(_resolve_decimals(w3, chain_id, token_out, sem))
    block_task = asyncio.create_task(w3.eth.block_number)
    tier_tasks = [
        asyncio.create_task(_quote_one_tier(quoter, token_in, token_out, amount_in_int, fee, sem)) for fee in _FEE_TIERS
    ]

    decimals_in = await decimals_in_task
    decimals_out = await decimals_out_task
    try:
        block_number = int(await block_task)
    except Exception as exc:
        logger.warning("block_number fetch failed: %s", exc)
        block_number = 0

    tier_quotes: list[TierQuote] = await asyncio.gather(*tier_tasks)

    # Select the winner: highest successful amount_out.
    successes = [q for q in tier_quotes if q.success and q.amount_out is not None]
    if not successes:
        return GetDexQuoteOutput(
            chain_id=chain_id,
            token_in=token_in,
            token_out=token_out,
            amount_in=str(amount_in_int),
            amount_in_decimals=decimals_in,
            amount_out="0",
            amount_out_decimals=decimals_out,
            amount_out_human="0",
            effective_rate="0",
            fee_tier_used=None,
            quotes_per_tier=tier_quotes,
            no_liquidity=True,
            block_number=block_number,
        ).model_dump()

    best = max(successes, key=lambda q: int(q.amount_out or "0"))
    best_amount_out = int(best.amount_out or "0")

    amount_in_human = Decimal(amount_in_int) / (Decimal(10) ** decimals_in)
    amount_out_human_dec = Decimal(best_amount_out) / (Decimal(10) ** decimals_out)
    if amount_in_human > 0:
        effective_rate = format((amount_out_human_dec / amount_in_human).normalize(), "f")
    else:
        effective_rate = "0"

    return GetDexQuoteOutput(
        chain_id=chain_id,
        token_in=token_in,
        token_out=token_out,
        amount_in=str(amount_in_int),
        amount_in_decimals=decimals_in,
        amount_out=str(best_amount_out),
        amount_out_decimals=decimals_out,
        amount_out_human=_format_human(best_amount_out, decimals_out),
        effective_rate=effective_rate,
        fee_tier_used=best.fee_tier,
        quotes_per_tier=tier_quotes,
        no_liquidity=False,
        block_number=block_number,
    ).model_dump()


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_dex_quote",
    version="1.0.0",
    description=(
        "Get the best Uniswap v3 swap quote on Ethereum (chain_id=1) or Base "
        "(chain_id=8453) via direct on-chain QuoterV2 calls. Queries all four fee "
        "tiers (100/500/3000/10000 bps) in parallel and returns the tier with the "
        "highest output amount, along with per-tier breakdown. Inputs are raw "
        "uint256 amounts and EIP-55 checksummed addresses; native ETH is not "
        "quoted directly — pass the WETH address. Returns no_liquidity=true when "
        "no pool exists for the pair. Point-in-time quote at the returned "
        "block_number; do not cache."
    ),
    tags=["web3", "defi", "uniswap", "dex", "quote", "trading"],
    input_schema=GetDexQuoteInput,
    output_schema=GetDexQuoteOutput,
    implementation=get_dex_quote,
)
