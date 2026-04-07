# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""get_transaction – fetch transaction details + receipt by hash."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from web3 import Web3

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetTransactionInput(BaseModel):
    tx_hash: str = Field(..., description="Transaction hash (0x…)")
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")


class GetTransactionOutput(BaseModel):
    tx_hash: str
    from_address: str
    to_address: str | None
    value_eth: str
    gas_used: int | None
    gas_price_gwei: str | None
    status: int | None = Field(None, description="1=success, 0=revert, None=pending")
    block_number: int | None
    chain_id: int


# ─── Implementation ──────────────────────────────────────────────────────────


async def get_transaction(tx_hash: str, chain_id: int = 1) -> dict[str, Any]:
    """Return transaction details and receipt status."""
    w3 = get_web3(chain_id)

    tx = await w3.eth.get_transaction(tx_hash)

    # Try to get receipt (may fail for pending txns)
    receipt = None
    try:
        receipt = await w3.eth.get_transaction_receipt(tx_hash)
    except Exception:
        pass

    value_eth = str(Web3.from_wei(tx.get("value", 0), "ether"))
    gas_price = tx.get("gasPrice")
    gas_price_gwei = str(Web3.from_wei(gas_price, "gwei")) if gas_price else None

    return {
        "tx_hash": tx_hash,
        "from_address": tx.get("from", ""),
        "to_address": tx.get("to"),
        "value_eth": value_eth,
        "gas_used": receipt.get("gasUsed") if receipt else None,
        "gas_price_gwei": gas_price_gwei,
        "status": receipt.get("status") if receipt else None,
        "block_number": tx.get("blockNumber"),
        "chain_id": chain_id,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_transaction",
    version="1.0.0",
    description="Get details and receipt for an Ethereum or Base transaction by hash.",
    tags=["web3", "ethereum", "transaction"],
    input_schema=GetTransactionInput,
    output_schema=GetTransactionOutput,
    implementation=get_transaction,
)
