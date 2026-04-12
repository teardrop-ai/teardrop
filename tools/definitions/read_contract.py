# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""read_contract – call view/pure functions on any smart contract."""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field
from web3 import Web3

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

_ALLOWED_MUTABILITY = {"view", "pure"}


def _serialize_value(val: Any) -> Any:
    """Convert Web3 return types to JSON-safe primitives."""
    if isinstance(val, bytes):
        return "0x" + val.hex()
    if isinstance(val, int) and (val > 2**53 or val < -(2**53)):
        return str(val)
    if isinstance(val, (list, tuple)):
        return [_serialize_value(v) for v in val]
    return val


# ─── Schemas ──────────────────────────────────────────────────────────────────


class ReadContractInput(BaseModel):
    contract_address: str = Field(..., description="Contract address (0x…)")
    abi_fragment: str = Field(
        ...,
        description=(
            "JSON array containing the ABI for the function to call. "
            "Only view/pure functions are allowed."
        ),
    )
    function_name: str = Field(..., description="Name of the function to call")
    args: list[Any] = Field(
        default_factory=list, description="Positional arguments for the function call"
    )
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")


class ReadContractOutput(BaseModel):
    contract_address: str
    function_name: str
    result: Any
    chain_id: int


# ─── Implementation ──────────────────────────────────────────────────────────


async def read_contract(
    contract_address: str,
    abi_fragment: str,
    function_name: str,
    args: list[Any] | None = None,
    chain_id: int = 1,
) -> dict[str, Any]:
    """Call a view/pure function on any smart contract and return the result."""
    args = args or []

    # Parse ABI
    try:
        abi = json.loads(abi_fragment)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ABI JSON: {exc}") from exc

    if not isinstance(abi, list):
        raise ValueError("ABI fragment must be a JSON array")

    # Validate that the target function is view or pure
    fn_abi = None
    for entry in abi:
        if (
            entry.get("type", "function") == "function"
            and entry.get("name") == function_name
        ):
            fn_abi = entry
            break

    if fn_abi is None:
        raise ValueError(
            f"Function '{function_name}' not found in provided ABI fragment"
        )

    mutability = fn_abi.get("stateMutability", "nonpayable")
    if mutability not in _ALLOWED_MUTABILITY:
        raise ValueError(
            f"Function '{function_name}' has stateMutability='{mutability}'. "
            f"Only view/pure functions are allowed for safety."
        )

    w3 = get_web3(chain_id)
    address = Web3.to_checksum_address(contract_address)
    contract = w3.eth.contract(address=address, abi=abi)

    fn = contract.functions[function_name]
    raw = await fn(*args).call()

    return {
        "contract_address": address,
        "function_name": function_name,
        "result": _serialize_value(raw),
        "chain_id": chain_id,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="read_contract",
    version="1.0.0",
    description=(
        "Call any view/pure function on a smart contract. Provide the ABI fragment "
        "and function name. State-changing functions (payable/nonpayable) are rejected."
    ),
    tags=["web3", "ethereum", "contract", "abi"],
    input_schema=ReadContractInput,
    output_schema=ReadContractOutput,
    implementation=read_contract,
)
