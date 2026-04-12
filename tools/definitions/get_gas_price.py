# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_gas_price – current gas prices on Ethereum or Base."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from web3 import Web3

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetGasPriceInput(BaseModel):
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")


class GetGasPriceOutput(BaseModel):
    chain_id: int
    gas_price_gwei: str
    base_fee_gwei: str | None
    priority_fee_gwei: str | None


# ─── Implementation ──────────────────────────────────────────────────────────


async def get_gas_price(chain_id: int = 1) -> dict[str, Any]:
    """Return current gas price and fee components for a chain."""
    w3 = get_web3(chain_id)

    gas_price_wei = await w3.eth.gas_price
    gas_price_gwei = str(Web3.from_wei(gas_price_wei, "gwei"))

    # Fetch latest block for base fee
    base_fee_gwei: str | None = None
    priority_fee_gwei: str | None = None
    try:
        block = await w3.eth.get_block("latest")
        base_fee = block.get("baseFeePerGas")
        if base_fee is not None:
            base_fee_gwei = str(Web3.from_wei(base_fee, "gwei"))
            # Priority fee = gas_price - base_fee (approximate)
            priority = gas_price_wei - base_fee
            if priority > 0:
                priority_fee_gwei = str(Web3.from_wei(priority, "gwei"))
    except Exception:
        pass

    return {
        "chain_id": chain_id,
        "gas_price_gwei": gas_price_gwei,
        "base_fee_gwei": base_fee_gwei,
        "priority_fee_gwei": priority_fee_gwei,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_gas_price",
    version="1.0.0",
    description="Get current gas price (base fee + priority fee) on Ethereum or Base.",
    tags=["web3", "ethereum", "gas", "fees"],
    input_schema=GetGasPriceInput,
    output_schema=GetGasPriceOutput,
    implementation=get_gas_price,
)
