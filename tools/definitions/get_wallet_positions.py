# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""All-chain wallet positions backed by DeBank Cloud portfolio data."""

from __future__ import annotations

import math
import time
from typing import Any

from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from tools._internals._debank import get_wallet_positions as fetch_wallet_positions
from tools._internals.provenance import DataProvenance, cache_age_seconds, utc_now_iso
from tools.registry import ToolDefinition


class GetWalletPositionsInput(BaseModel):
    wallet_address: str = Field(..., description="EVM wallet address (0x…)")
    include_net_worth: bool = Field(
        default=True,
        description="Include all-chain net worth and per-chain balances; disable to avoid that additional provider request.",
    )
    include_token_balances: bool = Field(
        default=False,
        description="Include DeBank's complete cross-chain token balance list; adds a billable provider request.",
    )

    @field_validator("wallet_address")
    @classmethod
    def _validate_wallet_address(cls, value: str) -> str:
        try:
            return Web3.to_checksum_address(value.strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("wallet_address must be a valid 20-byte EVM address") from exc


class WalletToken(BaseModel):
    token_id: str
    chain_id: str | None = None
    name: str | None = None
    symbol: str | None = None
    display_symbol: str | None = None
    decimals: int | None = None
    amount: float | None = None
    raw_amount: str | None = None
    price_usd: float | None = None
    usd_value: float | None = None
    protocol_id: str | None = None
    is_verified: bool | None = None


class WalletPositionItem(BaseModel):
    name: str | None = None
    description: str | None = None
    update_at: int | None = None
    asset_usd_value: float | None = None
    debt_usd_value: float | None = None
    net_usd_value: float | None = None
    detail_types: list[str] = Field(default_factory=list)
    token_lists: dict[str, list[WalletToken]] = Field(
        default_factory=dict,
        description="Protocol-specific token lists keyed by their DeBank detail field.",
    )


class WalletProtocolPosition(BaseModel):
    protocol_id: str
    chain_id: str | None = None
    protocol_name: str | None = None
    site_url: str | None = None
    has_supported_portfolio: bool | None = None
    protocol_tvl_usd: float | None = None
    items: list[WalletPositionItem] = Field(default_factory=list)


class WalletChainBalance(BaseModel):
    chain_id: str
    name: str | None = None
    community_id: int | None = None
    usd_value: float | None = None


class WalletPositionsError(BaseModel):
    operation: str
    error_type: str
    message: str


class GetWalletPositionsOutput(BaseModel):
    wallet_address: str
    include_net_worth: bool
    include_token_balances: bool
    total_net_worth_usd: float | None = None
    chain_balances: list[WalletChainBalance] = Field(default_factory=list)
    positions: list[WalletProtocolPosition] = Field(default_factory=list)
    token_balances: list[WalletToken] = Field(default_factory=list)
    data_complete: bool
    errors: list[WalletPositionsError] = Field(default_factory=list)
    provenance: DataProvenance
    staleness_seconds: float | None = Field(
        default=None,
        description=(
            "Max age in seconds of the underlying DeBank position data (from per-item update_at), or None when unavailable."
        ),
    )
    note: str = (
        "Positions are sourced from DeBank Cloud portfolio analytics and may be stale; they are not block-accurate. "
        "staleness_seconds reports the oldest underlying position update. "
        "Use get_defi_positions or other raw-RPC tools for liquidation, quote, and transaction-critical checks."
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


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _normalize_token(raw: Any) -> WalletToken | None:
    if not isinstance(raw, dict):
        return None
    token_id = _text(raw.get("id")) or _text(raw.get("token_id"))
    if token_id is None:
        return None

    amount = _number(raw.get("amount"))
    price_usd = _number(raw.get("price"))
    usd_value = _number(raw.get("usd_value"))
    if usd_value is None and amount is not None and price_usd is not None:
        usd_value = round(amount * price_usd, 8)

    raw_amount = raw.get("raw_amount")
    return WalletToken(
        token_id=token_id,
        chain_id=_text(raw.get("chain")),
        name=_text(raw.get("name")),
        symbol=_text(raw.get("symbol")),
        display_symbol=_text(raw.get("display_symbol")) or _text(raw.get("optimized_symbol")),
        decimals=_integer(raw.get("decimals")),
        amount=amount,
        raw_amount=str(raw_amount) if raw_amount is not None else None,
        price_usd=price_usd,
        usd_value=usd_value,
        protocol_id=_text(raw.get("protocol_id")) or _text(raw.get("app_id")),
        is_verified=_boolean(raw.get("is_verified")),
    )


def _normalize_token_lists(detail: Any) -> dict[str, list[WalletToken]]:
    if not isinstance(detail, dict):
        return {}
    token_lists: dict[str, list[WalletToken]] = {}
    for key, value in detail.items():
        if not isinstance(key, str) or not key.endswith("_token_list") or not isinstance(value, list):
            continue
        normalized = [token for item in value if (token := _normalize_token(item)) is not None]
        if normalized:
            token_lists[key] = normalized
    return token_lists


def _normalize_position_item(raw: Any) -> WalletPositionItem:
    if not isinstance(raw, dict):
        return WalletPositionItem()
    stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
    detail = raw.get("detail")
    detail_types = raw.get("detail_types")
    return WalletPositionItem(
        name=_text(raw.get("name")),
        description=_text(raw.get("description")),
        update_at=_integer(raw.get("update_at")),
        asset_usd_value=_number(stats.get("asset_usd_value")),
        debt_usd_value=_number(stats.get("debt_usd_value")),
        net_usd_value=_number(stats.get("net_usd_value")),
        detail_types=[item for item in detail_types if isinstance(item, str)] if isinstance(detail_types, list) else [],
        token_lists=_normalize_token_lists(detail),
    )


def _normalize_protocol_position(raw: Any) -> WalletProtocolPosition | None:
    if not isinstance(raw, dict):
        return None
    protocol_id = _text(raw.get("id"))
    if protocol_id is None:
        return None
    item_list = raw.get("portfolio_item_list")
    items = [_normalize_position_item(item) for item in item_list] if isinstance(item_list, list) else []
    return WalletProtocolPosition(
        protocol_id=protocol_id,
        chain_id=_text(raw.get("chain")),
        protocol_name=_text(raw.get("name")),
        site_url=_text(raw.get("site_url")),
        has_supported_portfolio=_boolean(raw.get("has_supported_portfolio")),
        protocol_tvl_usd=_number(raw.get("tvl")),
        items=items,
    )


def _max_staleness_seconds(positions: list[WalletProtocolPosition]) -> float | None:
    """Return the max age (seconds) of the oldest position-item update_at, or None."""
    now = time.time()
    oldest: float | None = None
    for position in positions:
        for item in position.items:
            if item.update_at is None:
                continue
            age = now - item.update_at
            if oldest is None or age > oldest:
                oldest = age
    return oldest


def _normalize_chain_balances(raw: Any) -> list[WalletChainBalance]:
    if not isinstance(raw, list):
        return []
    result: list[WalletChainBalance] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        chain_id = _text(item.get("id"))
        if chain_id is None:
            continue
        result.append(
            WalletChainBalance(
                chain_id=chain_id,
                name=_text(item.get("name")),
                community_id=_integer(item.get("community_id")),
                usd_value=_number(item.get("usd_value")),
            )
        )
    return result


async def get_wallet_positions(
    wallet_address: str,
    include_net_worth: bool = True,
    include_token_balances: bool = False,
) -> dict[str, Any]:
    """Return all-chain DeBank protocol positions for an EVM wallet."""
    wallet = GetWalletPositionsInput(
        wallet_address=wallet_address,
        include_net_worth=include_net_worth,
        include_token_balances=include_token_balances,
    )
    snapshot = await fetch_wallet_positions(
        wallet.wallet_address,
        include_net_worth=wallet.include_net_worth,
        include_token_balances=wallet.include_token_balances,
    )

    errors = [
        WalletPositionsError(
            operation=error.operation,
            error_type=error.error_type,
            message=error.message,
        )
        for error in snapshot.errors
    ]
    total_balance = snapshot.total_balance or {}
    provenance = DataProvenance(
        provider="DeBank Cloud",
        source_urls=snapshot.source_urls,
        retrieved_at=utc_now_iso(),
        source_fetched_at=snapshot.source_fetched_at,
        cache_hit=snapshot.cache_hit,
        cache_age_seconds=cache_age_seconds(snapshot.source_fetched_at) if snapshot.cache_hit else None,
        cache_ttl_seconds=60,
    )
    positions = [
        position
        for raw_position in snapshot.protocol_positions
        if (position := _normalize_protocol_position(raw_position)) is not None
    ]
    output = GetWalletPositionsOutput(
        wallet_address=wallet.wallet_address,
        include_net_worth=wallet.include_net_worth,
        include_token_balances=wallet.include_token_balances,
        total_net_worth_usd=_number(total_balance.get("total_usd_value")) if wallet.include_net_worth else None,
        chain_balances=_normalize_chain_balances(total_balance.get("chain_list")) if wallet.include_net_worth else [],
        positions=positions,
        token_balances=[token for raw_token in snapshot.token_balances if (token := _normalize_token(raw_token)) is not None],
        data_complete=not errors,
        errors=errors,
        provenance=provenance,
        staleness_seconds=_max_staleness_seconds(positions),
    )
    return output.model_dump()


TOOL = ToolDefinition(
    name="get_wallet_positions",
    version="1.0.0",
    description=(
        "Get a wallet's DeFi positions across all DeBank-supported chains and protocols. "
        "Returns protocol-level positions, asset/debt/net USD values, token lists, and optional all-chain net worth. "
        "Set include_token_balances=true to also return DeBank's complete cross-chain wallet token list. "
        "This covers substantially more protocols and chains than the block-accurate get_defi_positions tool. "
        "Use raw-RPC tools for liquidation, swap quotes, or other transaction-critical questions."
    ),
    use_when=(
        "Use for broad wallet discovery, portfolio allocation, protocol exposure, or cross-chain position questions. "
        "Set include_token_balances=true when complete wallet token discovery is required; otherwise the response "
        "only includes tokens attached to protocol positions. "
        "Use get_defi_positions when the user needs a block-accurate Aave, Compound, Uniswap, or Lido snapshot."
    ),
    limitations=(
        "DeBank portfolio data is third-party analytics and may be stale, including occasional long refresh delays. "
        "It is read-only, EVM wallet oriented, and does not prove current liquidation or execution state. "
        "include_token_balances adds a separate billable DeBank request."
    ),
    alternatives=["get_defi_positions", "get_wallet_portfolio", "get_token_approvals"],
    tags=["web3", "defi", "portfolio", "wallet", "cross-chain", "debank"],
    input_schema=GetWalletPositionsInput,
    output_schema=GetWalletPositionsOutput,
    implementation=get_wallet_positions,
)
