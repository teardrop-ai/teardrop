# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Cross-chain token authorization exposure backed by DeBank Cloud."""

from __future__ import annotations

import math
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from tools._internals._debank import (
    DebankEndpointSnapshot,
    DebankError,
)
from tools._internals._debank import (
    get_wallet_approvals as fetch_wallet_approvals,
)
from tools._internals.provenance import DataProvenance, cache_age_seconds, utc_now_iso
from tools.registry import ToolDefinition

_CHAIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class GetWalletApprovalsInput(BaseModel):
    wallet_address: str = Field(..., description="EVM wallet address (0x-prefixed)")
    chain_id: str = Field(..., description="DeBank chain identifier, for example eth, arb, or base")

    @field_validator("wallet_address")
    @classmethod
    def _validate_wallet_address(cls, value: str) -> str:
        try:
            return Web3.to_checksum_address(value.strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("wallet_address must be a valid 20-byte EVM address") from exc

    @field_validator("chain_id", mode="before")
    @classmethod
    def _validate_chain_id(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("chain_id must be a DeBank chain identifier")
        normalized = value.strip().lower()
        if not _CHAIN_ID_RE.fullmatch(normalized):
            raise ValueError("chain_id must contain only lowercase letters, numbers, hyphens, or underscores")
        return normalized


class WalletApprovalSpender(BaseModel):
    id: str
    value: str | int | float | None = None
    exposure_usd: float | None = None
    protocol: dict[str, Any] | None = None
    is_contract: bool | None = None
    is_open_source: bool | None = None
    is_hacked: bool | None = None
    is_abandoned: bool | None = None


class WalletApprovalToken(BaseModel):
    id: str
    name: str | None = None
    symbol: str | None = None
    logo_url: str | None = None
    chain: str | None = None
    price: float | None = None
    balance: float | None = None
    spenders: list[WalletApprovalSpender] = Field(default_factory=list)
    sum_exposure_usd: float | None = None
    exposure_balance: float | None = None


class WalletApprovalsError(BaseModel):
    operation: str
    error_type: str
    message: str


class GetWalletApprovalsOutput(BaseModel):
    wallet_address: str
    chain_id: str
    approvals: list[WalletApprovalToken] = Field(default_factory=list)
    data_complete: bool
    errors: list[WalletApprovalsError] = Field(default_factory=list)
    provenance: DataProvenance
    note: str = (
        "DeBank authorization exposure is analytics data for one chain and may be stale. "
        "It does not inspect NFT approvals or off-chain Permit2 sub-permits."
    )


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _approval_value(value: Any) -> str | int | float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return value


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _normalize_spender(raw: Any) -> WalletApprovalSpender | None:
    if not isinstance(raw, dict):
        return None
    spender_id = _text(raw.get("id"))
    if spender_id is None:
        return None
    protocol = raw.get("protocol")
    return WalletApprovalSpender(
        id=spender_id,
        value=_approval_value(raw.get("value")),
        exposure_usd=_number(raw.get("exposure_usd")),
        protocol=dict(protocol) if isinstance(protocol, dict) else None,
        is_contract=_boolean(raw.get("is_contract")),
        is_open_source=_boolean(raw.get("is_open_source")),
        is_hacked=_boolean(raw.get("is_hacked")),
        is_abandoned=_boolean(raw.get("is_abandoned")),
    )


def _normalize_token(raw: Any) -> WalletApprovalToken | None:
    if not isinstance(raw, dict):
        return None
    token_id = _text(raw.get("id"))
    if token_id is None:
        return None
    raw_spenders = raw.get("spenders")
    spenders = [spender for item in raw_spenders or [] if (spender := _normalize_spender(item)) is not None]
    return WalletApprovalToken(
        id=token_id,
        name=_text(raw.get("name")),
        symbol=_text(raw.get("symbol")),
        logo_url=_text(raw.get("logo_url")),
        chain=_text(raw.get("chain")),
        price=_number(raw.get("price")),
        balance=_number(raw.get("balance")),
        spenders=spenders,
        sum_exposure_usd=_number(raw.get("sum_exposure_usd")),
        exposure_balance=_number(raw.get("exposure_balance")),
    )


def _error_from_debank(error: DebankError) -> WalletApprovalsError:
    return WalletApprovalsError(operation=error.operation, error_type=error.error_type, message=error.message)


def _provenance(snapshot: DebankEndpointSnapshot, source_fetched_at: str | None) -> DataProvenance:
    return DataProvenance(
        provider="DeBank Cloud",
        source_urls=[snapshot.source_url],
        retrieved_at=utc_now_iso(),
        source_fetched_at=source_fetched_at,
        cache_hit=snapshot.cache_hit,
        cache_age_seconds=cache_age_seconds(snapshot.source_fetched_at)
        if snapshot.cache_hit and source_fetched_at is not None
        else None,
        cache_ttl_seconds=60,
    )


async def get_wallet_approvals(wallet_address: str, chain_id: str) -> dict[str, Any]:
    """Return DeBank's token authorization exposure for one chain."""
    request = GetWalletApprovalsInput(wallet_address=wallet_address, chain_id=chain_id)
    snapshot = await fetch_wallet_approvals(request.wallet_address, request.chain_id)
    errors: list[WalletApprovalsError] = []
    if snapshot.error is not None:
        errors.append(_error_from_debank(snapshot.error))

    approvals: list[WalletApprovalToken] = []
    if snapshot.error is None and not isinstance(snapshot.payload, list):
        errors.append(
            WalletApprovalsError(
                operation="token_authorized_list",
                error_type="malformed_response",
                message="Expected a JSON list",
            )
        )
    elif snapshot.error is None:
        for raw_item in snapshot.payload:
            if (
                isinstance(raw_item, dict)
                and raw_item.get("spenders") is not None
                and not isinstance(raw_item.get("spenders"), list)
            ):
                errors.append(
                    WalletApprovalsError(
                        operation="token_authorized_list",
                        error_type="malformed_response",
                        message="Skipped an authorization entry with invalid spenders",
                    )
                )
                continue
            normalized = _normalize_token(raw_item)
            if normalized is None:
                errors.append(
                    WalletApprovalsError(
                        operation="token_authorized_list",
                        error_type="malformed_response",
                        message="Skipped an invalid authorization entry",
                    )
                )
                continue
            approvals.append(normalized)

    return GetWalletApprovalsOutput(
        wallet_address=request.wallet_address,
        chain_id=request.chain_id,
        approvals=approvals,
        data_complete=not errors,
        errors=errors,
        provenance=_provenance(snapshot, snapshot.source_fetched_at if not errors else None),
    ).model_dump()


TOOL = ToolDefinition(
    name="get_wallet_approvals",
    version="1.0.0",
    description=(
        "Inspect a wallet's current ERC-20 token authorization exposure on one DeBank-supported chain. "
        "Returns discovered spenders, USD exposure, protocol attribution, and hacked or abandoned protocol flags. "
        "This is broader than the curated block-accurate get_token_approvals tool."
    ),
    use_when=(
        "Use when broad spender discovery or cross-chain security screening is needed. "
        "Call once per chain because DeBank requires a chain_id. Use get_token_approvals for a free, "
        "block-accurate Ethereum or Base scan of curated tokens and spenders."
    ),
    limitations=(
        "The result is third-party analytics and may be stale. It covers token approvals only, not NFT approvals "
        "or off-chain Permit2 sub-permits; each chain consumes a separate billable provider call."
    ),
    alternatives=["get_token_approvals"],
    tags=["web3", "wallet", "security", "approvals", "cross-chain", "debank"],
    input_schema=GetWalletApprovalsInput,
    output_schema=GetWalletApprovalsOutput,
    implementation=get_wallet_approvals,
)
