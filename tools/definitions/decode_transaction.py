# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""decode_transaction – decode transaction calldata into human-readable form."""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp
from pydantic import BaseModel, Field
from web3 import Web3

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# ─── Schemas ──────────────────────────────────────────────────────────────────


class DecodeTransactionInput(BaseModel):
    tx_hash: str = Field(..., description="Transaction hash (0x…)")
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")
    abi_json: str | None = Field(
        default=None,
        description="Optional ABI JSON array for decoding. If omitted, 4byte.directory is used.",
    )


class DecodeTransactionOutput(BaseModel):
    tx_hash: str
    from_address: str
    to_address: str | None
    value_eth: str
    function_selector: str | None
    function_name: str | None
    decoded_args: dict[str, Any] | None
    raw_calldata: str
    decode_source: str | None
    chain_id: int


# ─── 4byte.directory fallback ─────────────────────────────────────────────────


async def _lookup_4byte(selector: str) -> str | None:
    """Look up a 4-byte function selector via 4byte.directory. Returns text signature or None."""
    url = f"https://www.4byte.directory/api/v1/signatures/?hex_signature={selector}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", [])
                    if results:
                        # Return the most popular (first) match
                        return results[0].get("text_signature")
    except Exception as exc:
        logger.debug("4byte.directory lookup failed for %s: %s", selector, exc)
    return None


def _serialize_value(val: Any) -> Any:
    """Convert Web3 types to JSON-serializable values."""
    if isinstance(val, bytes):
        return "0x" + val.hex()
    if isinstance(val, int) and (val > 2**53 or val < -(2**53)):
        return str(val)
    if isinstance(val, (list, tuple)):
        return [_serialize_value(v) for v in val]
    return val


# ─── Implementation ──────────────────────────────────────────────────────────


async def decode_transaction(
    tx_hash: str, chain_id: int = 1, abi_json: str | None = None
) -> dict[str, Any]:
    """Decode a transaction's calldata into function name and arguments."""
    w3 = get_web3(chain_id)

    tx = await w3.eth.get_transaction(tx_hash)

    from_addr = tx.get("from", "")
    to_addr = tx.get("to")
    value_eth = str(Web3.from_wei(tx.get("value", 0), "ether"))
    calldata = tx.get("input", "0x")
    if isinstance(calldata, bytes):
        calldata = "0x" + calldata.hex()

    # No calldata = simple ETH transfer
    if not calldata or calldata == "0x" or len(calldata) < 10:
        return {
            "tx_hash": tx_hash,
            "from_address": from_addr,
            "to_address": to_addr,
            "value_eth": value_eth,
            "function_selector": None,
            "function_name": "transfer (native ETH)",
            "decoded_args": None,
            "raw_calldata": calldata,
            "decode_source": None,
            "chain_id": chain_id,
        }

    selector = calldata[:10]  # 0x + 4 bytes = 10 chars
    function_name: str | None = None
    decoded_args: dict[str, Any] | None = None
    decode_source: str | None = None

    # Try ABI-based decoding first
    if abi_json and to_addr:
        try:
            abi = json.loads(abi_json)
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(to_addr), abi=abi
            )
            func, args = contract.decode_function_input(calldata)
            function_name = func.fn_name
            decoded_args = {k: _serialize_value(v) for k, v in dict(args).items()}
            decode_source = "provided_abi"
        except Exception as exc:
            logger.debug("ABI decode failed: %s", exc)

    # Fallback to 4byte.directory
    if function_name is None:
        sig = await _lookup_4byte(selector)
        if sig:
            function_name = sig
            decode_source = "4byte.directory"

    return {
        "tx_hash": tx_hash,
        "from_address": from_addr,
        "to_address": to_addr,
        "value_eth": value_eth,
        "function_selector": selector,
        "function_name": function_name,
        "decoded_args": decoded_args,
        "raw_calldata": calldata,
        "decode_source": decode_source,
        "chain_id": chain_id,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="decode_transaction",
    version="1.0.0",
    description=(
        "Decode a transaction's calldata into a human-readable function name and arguments. "
        "Optionally provide an ABI for precise decoding; otherwise uses 4byte.directory."
    ),
    tags=["web3", "ethereum", "transaction", "decode", "calldata"],
    input_schema=DecodeTransactionInput,
    output_schema=DecodeTransactionOutput,
    implementation=decode_transaction,
)
