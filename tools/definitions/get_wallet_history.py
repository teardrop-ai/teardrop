# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Decoded cross-chain wallet activity backed by DeBank Cloud."""

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
    get_wallet_history as fetch_wallet_history,
)
from tools._internals.provenance import DataProvenance, cache_age_seconds, utc_now_iso
from tools.registry import ToolDefinition

_CHAIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class GetWalletHistoryInput(BaseModel):
    wallet_address: str = Field(..., description="EVM wallet address (0x-prefixed)")
    chain_ids: list[str] | None = Field(
        default=None,
        max_length=50,
        description="Optional DeBank chain identifiers; omit to query all supported chains.",
    )
    start_time: int | None = Field(
        default=None,
        ge=0,
        description="Return entries earlier than this Unix timestamp for cursor pagination.",
    )
    page_count: int = Field(default=20, ge=1, le=20, description="Number of history entries to request (maximum 20).")

    @field_validator("wallet_address")
    @classmethod
    def _validate_wallet_address(cls, value: str) -> str:
        try:
            return Web3.to_checksum_address(value.strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("wallet_address must be a valid 20-byte EVM address") from exc

    @field_validator("chain_ids", mode="before")
    @classmethod
    def _validate_chain_ids(cls, value: Any) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list) or not value:
            raise ValueError("chain_ids must be a non-empty list when provided")
        if len(value) > 50:
            raise ValueError("chain_ids list exceeds 50-chain limit")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("chain_ids must contain DeBank chain identifiers")
            chain_id = item.strip().lower()
            if not _CHAIN_ID_RE.fullmatch(chain_id):
                raise ValueError("chain_ids contains an invalid DeBank chain identifier")
            if chain_id not in normalized:
                normalized.append(chain_id)
        return normalized


class WalletHistoryError(BaseModel):
    operation: str
    error_type: str
    message: str


class GetWalletHistoryOutput(BaseModel):
    wallet_address: str
    chain_ids: list[str] | None
    start_time: int | None
    page_count: int
    history_list: list[dict[str, Any]] = Field(default_factory=list)
    project_dict: dict[str, dict[str, Any]] = Field(default_factory=dict)
    token_dict: dict[str, dict[str, Any]] = Field(default_factory=dict)
    cex_dict: dict[str, dict[str, Any]] = Field(default_factory=dict)
    next_cursor: int | None = None
    has_more: bool = False
    data_complete: bool
    errors: list[WalletHistoryError] = Field(default_factory=list)
    provenance: DataProvenance
    note: str = (
        "This is one page of DeBank's decoded activity history. It supports activity and gas analysis, "
        "but does not provide complete PnL or cost basis without historical execution prices."
    )


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def _error_from_debank(error: DebankError) -> WalletHistoryError:
    return WalletHistoryError(operation=error.operation, error_type=error.error_type, message=error.message)


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


def _normalize_dictionary(value: Any, operation: str) -> tuple[dict[str, dict[str, Any]], WalletHistoryError | None]:
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return {}, WalletHistoryError(operation=operation, error_type="malformed_response", message="Expected a JSON object")
    result: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, dict):
            result[key] = item
        else:
            return {}, WalletHistoryError(
                operation=operation,
                error_type="malformed_response",
                message="Dictionary contained an invalid entry",
            )
    return result, None


async def get_wallet_history(
    wallet_address: str,
    chain_ids: list[str] | None = None,
    start_time: int | None = None,
    page_count: int = 20,
) -> dict[str, Any]:
    """Return one validated page of DeBank's decoded wallet history."""
    request = GetWalletHistoryInput(
        wallet_address=wallet_address,
        chain_ids=chain_ids,
        start_time=start_time,
        page_count=page_count,
    )
    snapshot = await fetch_wallet_history(
        request.wallet_address,
        chain_ids=request.chain_ids,
        start_time=request.start_time,
        page_count=request.page_count,
    )
    errors: list[WalletHistoryError] = []
    if snapshot.error is not None:
        errors.append(_error_from_debank(snapshot.error))

    history_list: list[dict[str, Any]] = []
    project_dict: dict[str, dict[str, Any]] = {}
    token_dict: dict[str, dict[str, Any]] = {}
    cex_dict: dict[str, dict[str, Any]] = {}
    next_cursor: int | None = None
    has_more = False

    if snapshot.error is None and not isinstance(snapshot.payload, dict):
        errors.append(
            WalletHistoryError(
                operation="all_history_list",
                error_type="malformed_response",
                message="Expected a JSON object",
            )
        )
    elif snapshot.error is None:
        payload = snapshot.payload
        raw_history = payload.get("history_list")
        if not isinstance(raw_history, list):
            errors.append(
                WalletHistoryError(
                    operation="all_history_list",
                    error_type="malformed_response",
                    message="history_list must be a JSON list",
                )
            )
        else:
            for item in raw_history:
                if isinstance(item, dict):
                    history_list.append(item)
                else:
                    errors.append(
                        WalletHistoryError(
                            "all_history_list",
                            "malformed_response",
                            "Skipped an invalid history entry",
                        )
                    )

        for field_name, target_name in (
            ("project_dict", "project_dict"),
            ("token_dict", "token_dict"),
            ("cex_dict", "cex_dict"),
        ):
            normalized, error = _normalize_dictionary(payload.get(field_name), "all_history_list")
            if error is not None:
                errors.append(error)
            elif target_name == "project_dict":
                project_dict = normalized
            elif target_name == "token_dict":
                token_dict = normalized
            else:
                cex_dict = normalized

        if len(history_list) == request.page_count and history_list:
            candidate = _integer(history_list[-1].get("time_at"))
            if candidate is not None and (request.start_time is None or candidate < request.start_time):
                next_cursor = candidate
                has_more = True

    return GetWalletHistoryOutput(
        wallet_address=request.wallet_address,
        chain_ids=request.chain_ids,
        start_time=request.start_time,
        page_count=request.page_count,
        history_list=history_list,
        project_dict=project_dict,
        token_dict=token_dict,
        cex_dict=cex_dict,
        next_cursor=next_cursor,
        has_more=has_more,
        data_complete=not errors,
        errors=errors,
        provenance=_provenance(snapshot, snapshot.source_fetched_at if not errors else None),
    ).model_dump()


TOOL = ToolDefinition(
    name="get_wallet_history",
    version="1.0.0",
    description=(
        "Get one page of decoded transaction history for an EVM wallet across DeBank-supported chains. "
        "Returns send, receive, and approval categories with protocol, token, exchange, gas, and USD metadata. "
        "Use the returned next_cursor as start_time to page backward."
    ),
    use_when=(
        "Use for wallet activity discovery, protocol interaction history, exchange exposure, and gas analysis. "
        "Page backward with next_cursor for older activity. Use get_transaction or decode_transaction when one "
        "known transaction needs block-level details."
    ),
    limitations=(
        "DeBank returns at most 20 entries per call and the data may be stale. This tool is not a complete ledger, "
        "PnL engine, or cost-basis calculator because historical execution prices and all transfer semantics are not guaranteed."
    ),
    alternatives=["get_transaction", "decode_transaction"],
    tags=["web3", "wallet", "history", "activity", "cross-chain", "debank"],
    input_schema=GetWalletHistoryInput,
    output_schema=GetWalletHistoryOutput,
    implementation=get_wallet_history,
)
