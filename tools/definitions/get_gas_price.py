# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_gas_price – current EIP-1559 gas fees on Ethereum or Base."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, Field
from web3 import AsyncWeb3, Web3

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

# ─── TTL cache (per-chain) ────────────────────────────────────────────────────

# Gas prices change every block (~12 s Ethereum, ~2 s Base).  A 10-second cache
# prevents hammering the RPC on high-frequency agent loops while staying fresh
# enough for any practical transaction-timing decision.
_GAS_CACHE: dict[int, tuple[dict[str, Any], float]] = {}
_GAS_TTL = 10.0  # seconds

# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetGasPriceInput(BaseModel):
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")


class GetGasPriceOutput(BaseModel):
    chain_id: int
    gas_price_gwei: str
    base_fee_gwei: str | None
    priority_fee_gwei: str | None
    next_base_fee_gwei: str | None
    gas_used_ratio: float | None


# ─── Helpers ─────────────────────────────────────────────────────────────────


async def _get_max_priority_fee(w3: AsyncWeb3) -> int | None:
    """Return eth_maxPriorityFeePerGas, or None on pre-EIP-1559 chains."""
    try:
        return await w3.eth.max_priority_fee
    except Exception:
        return None


def _next_base_fee(base_fee: int, gas_used: int, gas_limit: int) -> int:
    """Compute next-block base fee using the EIP-1559 adjustment formula.

    Reference: https://eips.ethereum.org/EIPS/eip-1559
    """
    gas_target = gas_limit // 2
    if gas_used > gas_target:
        delta = base_fee * (gas_used - gas_target) // gas_target // 8
        return base_fee + delta
    elif gas_used < gas_target:
        delta = base_fee * (gas_target - gas_used) // gas_target // 8
        return max(0, base_fee - delta)
    return base_fee


# ─── Implementation ──────────────────────────────────────────────────────────


async def get_gas_price(chain_id: int = 1) -> dict[str, Any]:
    """Return current EIP-1559 fee components and next-block base fee estimate."""
    now = time.monotonic()
    cached = _GAS_CACHE.get(chain_id)
    if cached and now < cached[1]:
        return cached[0]

    w3 = get_web3(chain_id)

    # Fetch block and priority fee concurrently — single network latency budget.
    block, priority_fee_wei = await asyncio.gather(
        w3.eth.get_block("latest"),
        _get_max_priority_fee(w3),
    )

    base_fee: int | None = block.get("baseFeePerGas")
    gas_used: int = block.get("gasUsed", 0)
    gas_limit: int = block.get("gasLimit", 1)

    base_fee_gwei = str(Web3.from_wei(base_fee, "gwei")) if base_fee is not None else None
    priority_fee_gwei = str(Web3.from_wei(priority_fee_wei, "gwei")) if priority_fee_wei is not None else None

    # Next-block base fee (EIP-1559 formula).
    next_base_fee_gwei: str | None = None
    gas_used_ratio: float | None = None
    if base_fee is not None and gas_limit > 0:
        gas_used_ratio = round(gas_used / gas_limit, 4)
        next_base_fee_gwei = str(Web3.from_wei(_next_base_fee(base_fee, gas_used, gas_limit), "gwei"))

    # Suggested gas price = base_fee + priority_fee (EIP-1559).
    # Fall back to legacy eth_gasPrice on pre-EIP-1559 chains.
    if base_fee is not None and priority_fee_wei is not None:
        gas_price_gwei = str(Web3.from_wei(base_fee + priority_fee_wei, "gwei"))
    elif base_fee is not None:
        gas_price_gwei = base_fee_gwei  # type: ignore[assignment]
    else:
        legacy_price = await w3.eth.gas_price
        gas_price_gwei = str(Web3.from_wei(legacy_price, "gwei"))

    result: dict[str, Any] = {
        "chain_id": chain_id,
        "gas_price_gwei": gas_price_gwei,
        "base_fee_gwei": base_fee_gwei,
        "priority_fee_gwei": priority_fee_gwei,
        "next_base_fee_gwei": next_base_fee_gwei,
        "gas_used_ratio": gas_used_ratio,
    }
    _GAS_CACHE[chain_id] = (result, now + _GAS_TTL)
    return result


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_gas_price",
    version="1.0.0",
    description=(
        "Get current EIP-1559 gas fees on Ethereum or Base. Returns base fee, priority fee, "
        "and next-block base fee estimate (useful for timing transactions). "
        "gas_used_ratio indicates network congestion (>0.5 = busy, >0.9 = very congested). "
        "Results cached 10 seconds per chain."
    ),
    tags=["web3", "ethereum", "gas", "fees", "eip1559"],
    input_schema=GetGasPriceInput,
    output_schema=GetGasPriceOutput,
    implementation=get_gas_price,
)
