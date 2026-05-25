# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace data models and validation helpers."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel

_EIP55_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


class AuthorConfig(BaseModel):
    """Public representation of a tool author's marketplace configuration."""

    org_id: str
    settlement_wallet: str
    created_at: datetime
    updated_at: datetime


class AuthorEarning(BaseModel):
    """Single per-call earnings record."""

    id: str
    org_id: str
    tool_name: str
    caller_org_id: str
    amount_usdc: int
    author_share_usdc: int
    platform_share_usdc: int
    status: str  # "pending" | "settled" | "failed"
    created_at: datetime


class AuthorWithdrawal(BaseModel):
    """Withdrawal request record."""

    id: str
    org_id: str
    amount_usdc: int
    tx_hash: str
    wallet: str
    status: str  # "pending" | "settled" | "failed" | "exhausted"
    created_at: datetime
    settled_at: datetime | None = None
    sweep_attempt_count: int = 0
    last_sweep_error: str = ""
    next_sweep_at: datetime | None = None


class MarketplaceTool(BaseModel):
    """Public representation of a tool listed in the marketplace catalog."""

    name: str
    qualified_name: str  # {org_slug}/{tool_name}
    display_name: str = ""
    description: str
    marketplace_description: str
    input_schema: dict[str, Any]
    cost_usdc: int
    author_org_name: str
    author_org_slug: str


class MarketplaceSubscription(BaseModel):
    """A subscription linking an org to a marketplace tool."""

    id: str
    org_id: str
    qualified_tool_name: str
    is_active: bool
    subscribed_at: datetime
    subscribed_schema_hash: str | None = None


def validate_eip55_address(address: str) -> str | None:
    """Validate an Ethereum address. Returns error message or None if valid."""
    if not _EIP55_PATTERN.match(address):
        return "Invalid Ethereum address format (expected 0x + 40 hex characters)"

    try:
        from web3 import Web3

        if address != Web3.to_checksum_address(address.lower()):
            return "Address fails EIP-55 checksum — use checksummed format"
    except Exception:
        return "Address checksum validation failed"

    if address == "0x" + "0" * 40:
        return "Zero address is not a valid settlement wallet"

    return None
