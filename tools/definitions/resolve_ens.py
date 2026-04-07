# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""resolve_ens – resolve an ENS name to an Ethereum address."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition


# ─── Schemas ──────────────────────────────────────────────────────────────────


class ResolveEnsInput(BaseModel):
    name: str = Field(..., description="ENS name (e.g. 'vitalik.eth')")


class ResolveEnsOutput(BaseModel):
    name: str
    address: str | None
    resolved: bool


# ─── Implementation ──────────────────────────────────────────────────────────


async def resolve_ens(name: str) -> dict[str, Any]:
    """Resolve an ENS name to an address (mainnet only)."""
    w3 = get_web3(chain_id=1)  # ENS lives on L1

    try:
        address = await w3.ens.address(name)  # type: ignore[union-attr]
    except Exception:
        address = None

    return {
        "name": name,
        "address": address,
        "resolved": address is not None,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="resolve_ens",
    version="1.0.0",
    description="Resolve an ENS name (e.g. vitalik.eth) to an Ethereum address. Mainnet only.",
    tags=["web3", "ethereum", "ens", "identity"],
    input_schema=ResolveEnsInput,
    output_schema=ResolveEnsOutput,
    implementation=resolve_ens,
)
