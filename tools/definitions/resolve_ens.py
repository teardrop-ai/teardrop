# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""resolve_ens – resolve ENS names to addresses and fetch on-chain profile metadata."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from tools.definitions._web3_helpers import get_web3
from tools.registry import ToolDefinition

# ─── Input validation helpers ────────────────────────────────────────────────

# Matches a bare 0x Ethereum address (40 hex chars).
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# ENS names must contain at least one dot and use the allowed character set.
# Reference: https://docs.ens.domains/resolution/names
_ENS_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*(\.[a-z0-9][a-z0-9\-]*)+$")

# ─── Schemas ──────────────────────────────────────────────────────────────────


class ResolveEnsInput(BaseModel):
    name: str = Field(
        ...,
        description=(
            "ENS name (e.g. 'vitalik.eth') to resolve to an address, "
            "or an Ethereum address (0x…) for reverse lookup to a primary ENS name."
        ),
        max_length=253,  # DNS max length — also the ENS practical limit
    )

    @field_validator("name")
    @classmethod
    def _validate_format(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be empty")
        # Allow 0x addresses (reverse lookup) or ENS name patterns.
        if not _ADDR_RE.match(stripped) and not _ENS_RE.match(stripped.lower()):
            raise ValueError("name must be a valid ENS name (e.g. 'vitalik.eth') or an Ethereum address (0x…)")
        return stripped


class ResolveEnsOutput(BaseModel):
    name: str | None
    address: str | None
    resolved: bool
    avatar: str | None = None
    error: str | None = None


# ─── Implementation ──────────────────────────────────────────────────────────


async def resolve_ens(name: str) -> dict[str, Any]:
    """Resolve an ENS name → address (forward) or address → ENS name (reverse)."""
    w3 = get_web3(chain_id=1)  # ENS lives on Ethereum mainnet

    # ── Reverse lookup: Ethereum address → primary ENS name ───────────────────
    if _ADDR_RE.match(name):
        checksum = Web3.to_checksum_address(name)
        ens_name: str | None = None
        error: str | None = None
        try:
            ens_name = await w3.ens.name(checksum)  # type: ignore[union-attr]
        except Exception as exc:
            error = str(exc)
        return {
            "name": ens_name,
            "address": checksum,
            "resolved": ens_name is not None,
            "avatar": None,
            "error": error,
        }

    # ── Forward lookup: ENS name → Ethereum address ───────────────────────────
    normalised = name.lower()
    address: str | None = None
    fwd_error: str | None = None
    try:
        raw = await w3.ens.address(normalised)  # type: ignore[union-attr]
        if raw is not None:
            address = Web3.to_checksum_address(raw)
    except Exception as exc:
        fwd_error = str(exc)

    # Avatar text record — gracefully skipped if ENS doesn't support it or is unreachable.
    avatar: str | None = None
    if address is not None:
        try:
            avatar = await w3.ens.get_text(normalised, "avatar")  # type: ignore[union-attr]
        except Exception:
            pass

    return {
        "name": normalised,
        "address": address,
        "resolved": address is not None,
        "avatar": avatar,
        "error": fwd_error,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="resolve_ens",
    version="1.0.0",
    description=(
        "Resolve an ENS name (e.g. 'vitalik.eth') to an Ethereum address, "
        "or pass an Ethereum address for reverse lookup to its primary ENS name. "
        "Also returns the avatar text record when available. Mainnet only."
    ),
    tags=["web3", "ethereum", "ens", "identity"],
    input_schema=ResolveEnsInput,
    output_schema=ResolveEnsOutput,
    implementation=resolve_ens,
)
