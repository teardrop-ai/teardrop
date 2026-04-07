# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""get_eth_balance – fetch native ETH/Base-ETH balance for an address."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from web3 import Web3

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetEthBalanceInput(BaseModel):
    address: str = Field(..., description="Ethereum address (0x…)")
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")


class GetEthBalanceOutput(BaseModel):
    address: str
    balance_wei: str
    balance_eth: str
    chain_id: int


# ─── Implementation ──────────────────────────────────────────────────────────


async def get_eth_balance(address: str, chain_id: int = 1) -> dict[str, Any]:
    """Return the native ETH balance in both wei and ether."""
    w3 = get_web3(chain_id)
    checksum = Web3.to_checksum_address(address)
    balance_wei = await w3.eth.get_balance(checksum)
    balance_eth = Web3.from_wei(balance_wei, "ether")
    return {
        "address": checksum,
        "balance_wei": str(balance_wei),
        "balance_eth": str(balance_eth),
        "chain_id": chain_id,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_eth_balance",
    version="1.0.0",
    description="Get the native ETH balance of an Ethereum or Base address.",
    tags=["web3", "ethereum", "balance"],
    input_schema=GetEthBalanceInput,
    output_schema=GetEthBalanceOutput,
    implementation=get_eth_balance,
)
