# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""get_block – fetch block details by number, hash, or 'latest'."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from web3 import Web3

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition


# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetBlockInput(BaseModel):
    block_identifier: str = Field(
        default="latest",
        description="Block number, block hash, or 'latest'/'earliest'/'pending'",
    )
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")


class GetBlockOutput(BaseModel):
    number: int
    hash: str
    timestamp: int
    transaction_count: int
    gas_used: int
    base_fee_gwei: str | None


# ─── Implementation ──────────────────────────────────────────────────────────


async def get_block(
    block_identifier: str = "latest",
    chain_id: int = 1,
) -> dict[str, Any]:
    """Return block details for a given block identifier."""
    w3 = get_web3(chain_id)

    # Coerce numeric strings to int
    identifier: int | str = block_identifier
    if block_identifier.isdigit():
        identifier = int(block_identifier)

    block = await w3.eth.get_block(identifier)

    base_fee = block.get("baseFeePerGas")
    base_fee_gwei = str(Web3.from_wei(base_fee, "gwei")) if base_fee else None

    return {
        "number": block["number"],
        "hash": block["hash"].hex() if isinstance(block["hash"], bytes) else str(block["hash"]),
        "timestamp": block["timestamp"],
        "transaction_count": len(block.get("transactions", [])),
        "gas_used": block["gasUsed"],
        "base_fee_gwei": base_fee_gwei,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_block",
    version="1.0.0",
    description="Get details for an Ethereum or Base block by number, hash, or 'latest'.",
    tags=["web3", "ethereum", "block"],
    input_schema=GetBlockInput,
    output_schema=GetBlockOutput,
    implementation=get_block,
)
