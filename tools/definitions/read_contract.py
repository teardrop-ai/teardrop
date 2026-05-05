# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""read_contract – call view/pure functions on any smart contract."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field
from web3 import Web3

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

_ALLOWED_MUTABILITY = {"view", "pure"}

# ABI fragments are bounded to prevent memory-exhaustion from malformed inputs.
# 65 536 chars covers any realistic single-function ABI with room to spare.
_ABI_MAX_LEN = 65_536


def _serialize_value(val: Any) -> Any:
    """Convert Web3 return types to JSON-safe primitives.

    Handles bytes, large ints, dicts (web3 AttributeDict / structs),
    and sequences recursively.
    """
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


def _get_function_mutability(fn_abi: dict) -> str:
    """Determine function mutability from ABI, supporting both old and new formats.

    Solidity 0.5.0+ uses explicit stateMutability: 'view', 'pure', 'payable', 'nonpayable'.
    Pre-0.5.0 contracts (including Vyper) use constant: true to indicate read-only functions.
    This helper maps both formats to modern mutability states.

    Args:
        fn_abi: Function ABI entry dict.

    Returns:
        Mutability state: 'view', 'pure', 'payable', or 'nonpayable'.
    """
    # Legacy format: constant: true means read-only (equivalent to 'view' in modern Solidity)
    if fn_abi.get("constant") is True:
        return "view"
    # Modern format: explicit stateMutability field (default: 'nonpayable')
    return fn_abi.get("stateMutability", "nonpayable")


# ─── Schemas ──────────────────────────────────────────────────────────────────


class ReadContractInput(BaseModel):
    contract_address: str = Field(..., description="Contract address (0x…)")
    abi_fragment: str = Field(
        ...,
        description=(
            "JSON array containing the ABI for the function to call. "
            "Supports both modern (stateMutability: view/pure) and legacy (constant: true) formats. "
            "Only read-only functions are allowed."
        ),
        max_length=_ABI_MAX_LEN,
    )
    function_name: str = Field(
        ...,
        description="Name of the function to call",
        max_length=200,
    )
    args_json: str | None = Field(
        default=None,
        description="Optional JSON array string of positional arguments for the function call. Use this instead of 'args' for complex types or when calling via Google/Gemini.",
        max_length=2000,
    )
    args: list[str | int | bool] = Field(
        default_factory=list,
        description="DEPRECATED: Positional arguments for the function call. Use 'args_json' for complex objects or Gemini compatibility.",
        max_length=50,
    )
    block_identifier: str = Field(
        default="latest",
        description="Block number, block hash, or 'latest'/'earliest'/'pending'",
        max_length=80,
    )
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")


class ReadContractOutput(BaseModel):
    contract_address: str
    function_name: str
    result: Any
    block_identifier: str
    chain_id: int


# ─── Implementation ──────────────────────────────────────────────────────────


async def read_contract(
    contract_address: str,
    abi_fragment: str,
    function_name: str,
    args_json: str | None = None,
    args: list[Any] | None = None,
    block_identifier: str = "latest",
    chain_id: int = 1,
) -> dict[str, Any]:
    """Call a view/pure function on any smart contract and return the result."""
    # Resolve arguments: prefer args_json if provided.
    final_args: list[Any] = []
    if args_json:
        try:
            final_args = json.loads(args_json)
            if not isinstance(final_args, list):
                raise ValueError("args_json must be a JSON array")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid args_json: {exc}") from exc
    elif args:
        final_args = args

    # Parse ABI
    try:
        abi = json.loads(abi_fragment)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ABI JSON: {exc}") from exc

    if not isinstance(abi, list):
        raise ValueError("ABI fragment must be a JSON array")

    # Validate that the target function exists and is view/pure
    fn_abi = None
    for entry in abi:
        if entry.get("type", "function") == "function" and entry.get("name") == function_name:
            fn_abi = entry
            break

    if fn_abi is None:
        raise ValueError(f"Function '{function_name}' not found in provided ABI fragment")

    mutability = _get_function_mutability(fn_abi)
    if mutability not in _ALLOWED_MUTABILITY:
        raise ValueError(
            f"Function '{function_name}' has stateMutability='{mutability}'. Only view/pure functions are allowed for safety."
        )

    w3 = get_web3(chain_id)
    address = Web3.to_checksum_address(contract_address)
    contract = w3.eth.contract(address=address, abi=abi)

    # Coerce block_identifier to int when it looks like a block number.
    block_id: int | str = block_identifier
    if block_identifier.isdigit():
        block_id = int(block_identifier)

    try:
        fn = contract.functions[function_name]
    except KeyError:
        raise ValueError(
            f"Function '{function_name}' not found in contract ABI — check that function_name matches the ABI exactly."
        )

    raw = await fn(*final_args).call(block_identifier=block_id)

    return {
        "contract_address": address,
        "function_name": function_name,
        "result": _serialize_value(raw),
        "block_identifier": block_identifier,
        "chain_id": chain_id,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="read_contract",
    version="1.0.0",
    description=(
        "Call any view/pure function on a smart contract and return the result. "
        "Provide the ABI fragment (JSON array) and function name. "
        "State-changing functions (payable/nonpayable) are rejected for safety. "
        "Supports historical queries via block_identifier (block number or 'latest')."
    ),
    tags=["web3", "ethereum", "contract", "abi", "defi"],
    input_schema=ReadContractInput,
    output_schema=ReadContractOutput,
    implementation=read_contract,
)
