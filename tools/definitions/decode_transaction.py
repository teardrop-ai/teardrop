# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""decode_transaction – decode transaction calldata into human-readable form."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

import aiohttp
from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

# Calldata can be enormous for multicall transactions.  Truncating at 8 192 chars
# (4 096 bytes of hex) keeps response sizes sane while preserving all practical
# function-decoding information.
_CALLDATA_MAX_CHARS = 8_192
_ABI_MAX_LEN = 65_536

_DECODE_NOTE = (
    "decoded_args values are raw ABI-decoded types; uint256 amounts have not been "
    "decimals-formatted \u2014 do not call read_contract or get_erc20_balance to interpret them "
    "unless the user explicitly asks. Addresses (from_address, to_address, decoded_args) are "
    "plain hex \u2014 do not call resolve_ens to label them unless the user explicitly asks."
)

_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

# ─── Schemas ──────────────────────────────────────────────────────────────────


class DecodeTransactionInput(BaseModel):
    tx_hash: str = Field(..., description="Transaction hash (0x… 64 hex chars)")
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")
    abi_json: str | None = Field(
        default=None,
        description="Optional ABI JSON array for decoding. If omitted, 4byte.directory is used.",
        max_length=_ABI_MAX_LEN,
    )

    @field_validator("tx_hash")
    @classmethod
    def _validate_tx_hash(cls, v: str) -> str:
        if not _TX_HASH_RE.match(v):
            raise ValueError("tx_hash must be a 0x-prefixed 64-character hex string (32-byte hash)")
        return v.lower()


class DecodeTransactionOutput(BaseModel):
    tx_hash: str
    from_address: str
    to_address: str | None
    value_eth: str
    status: int | None = Field(None, description="1=success, 0=revert, None=pending")
    gas_used: int | None
    block_number: int | None
    function_selector: str | None
    function_name: str | None
    decoded_args: dict[str, Any] | None
    raw_calldata: str
    decode_source: str | None
    chain_id: int
    note: str = _DECODE_NOTE


# ─── 4byte.directory fallback ─────────────────────────────────────────────────


async def _lookup_4byte(selector: str) -> str | None:
    """Look up a 4-byte function selector via 4byte.directory."""
    url = f"https://www.4byte.directory/api/v1/signatures/?hex_signature={selector}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", [])
                    if results:
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
    if isinstance(val, Mapping):
        # Covers both plain dict and web3.py AttributeDict (Solidity structs).
        return {k: _serialize_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_serialize_value(v) for v in val]
    return val


# ─── Implementation ──────────────────────────────────────────────────────────


async def decode_transaction(tx_hash: str, chain_id: int = 1, abi_json: str | None = None) -> dict[str, Any]:
    """Decode a transaction's calldata into function name and arguments."""
    w3 = get_web3(chain_id)

    # Fetch tx and receipt concurrently — receipt provides status + gas_used.
    tx, receipt = await asyncio.gather(
        w3.eth.get_transaction(tx_hash),
        _safe_get_receipt(w3, tx_hash),
    )

    from_addr: str = tx.get("from", "")
    to_addr: str | None = tx.get("to")
    value_eth = str(Web3.from_wei(tx.get("value", 0), "ether"))
    block_number: int | None = tx.get("blockNumber")

    status: int | None = receipt.get("status") if receipt else None
    gas_used: int | None = receipt.get("gasUsed") if receipt else None

    calldata = tx.get("input", "0x")
    if isinstance(calldata, bytes):
        calldata = "0x" + calldata.hex()

    # Truncate large calldata (e.g. multicall) before including in response.
    truncated_calldata = calldata[:_CALLDATA_MAX_CHARS]

    # Simple ETH transfer — no calldata to decode.
    if not calldata or calldata == "0x" or len(calldata) < 10:
        return {
            "tx_hash": tx_hash,
            "from_address": from_addr,
            "to_address": to_addr,
            "value_eth": value_eth,
            "status": status,
            "gas_used": gas_used,
            "block_number": block_number,
            "function_selector": None,
            "function_name": "transfer (native ETH)",
            "decoded_args": None,
            "raw_calldata": truncated_calldata,
            "decode_source": None,
            "chain_id": chain_id,
            "note": _DECODE_NOTE,
        }

    selector = calldata[:10]  # "0x" + 4 bytes = 10 chars
    function_name: str | None = None
    decoded_args: dict[str, Any] | None = None
    decode_source: str | None = None

    # ABI-based decoding takes priority over 4byte lookup.
    if abi_json and to_addr:
        try:
            abi = json.loads(abi_json)
            contract = w3.eth.contract(address=Web3.to_checksum_address(to_addr), abi=abi)
            func, args = contract.decode_function_input(calldata)
            function_name = func.fn_name
            decoded_args = {k: _serialize_value(v) for k, v in dict(args).items()}
            decode_source = "provided_abi"
        except Exception as exc:
            logger.debug("ABI decode failed: %s", exc)

    # Fall back to 4byte.directory signature database.
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
        "status": status,
        "gas_used": gas_used,
        "block_number": block_number,
        "function_selector": selector,
        "function_name": function_name,
        "decoded_args": decoded_args,
        "raw_calldata": truncated_calldata,
        "decode_source": decode_source,
        "chain_id": chain_id,
        "note": _DECODE_NOTE,
    }


async def _safe_get_receipt(w3: Any, tx_hash: str) -> dict[str, Any] | None:
    """Fetch transaction receipt, returning None for pending transactions."""
    try:
        return await w3.eth.get_transaction_receipt(tx_hash)
    except Exception:
        return None


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="decode_transaction",
    version="1.0.0",
    description=(
        "Decode a transaction's calldata into a human-readable function name and arguments. "
        "Also returns transaction status (1=success, 0=revert), gas used, and block number. "
        "Optionally provide an ABI for precise decoding; otherwise uses 4byte.directory. "
        "Supports Ethereum mainnet and Base."
    ),
    tags=["web3", "ethereum", "transaction", "decode", "calldata"],
    input_schema=DecodeTransactionInput,
    output_schema=DecodeTransactionOutput,
    implementation=decode_transaction,
)
