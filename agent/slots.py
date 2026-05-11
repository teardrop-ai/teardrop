# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Typed slot extraction for compact planner context.

Tool outputs remain in ToolMessages for auditability. Slots provide a stable,
compact facts view to reduce repeated long JSON re-ingestion by the planner.
"""

from __future__ import annotations

import json
from typing import Any


def _as_json(payload: str) -> dict[str, Any] | None:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _wallet_key(payload: dict[str, Any]) -> str:
    wallet = str(payload.get("wallet_address") or "").lower()
    chain = payload.get("chain_id")
    return f"{chain}:{wallet}" if wallet and chain is not None else wallet


def _write_get_wallet_portfolio(payload: dict[str, Any], slots: dict[str, Any]) -> dict[str, Any]:
    key = _wallet_key(payload)
    if not key:
        return slots
    balances = dict(slots.get("balances", {}))
    wallet_balances = dict(balances.get(key, {}))
    for holding in payload.get("holdings", []) or []:
        if not isinstance(holding, dict):
            continue
        symbol = str(holding.get("symbol") or "").upper()
        if not symbol:
            continue
        wallet_balances[symbol] = {
            "balance_formatted": str(holding.get("balance_formatted", "")),
            "value_usd": holding.get("value_usd"),
            "price_usd": holding.get("price_usd"),
        }
    if wallet_balances:
        balances[key] = wallet_balances
        slots["balances"] = balances
    return slots


def _write_get_erc20_balance(payload: dict[str, Any], slots: dict[str, Any]) -> dict[str, Any]:
    key = _wallet_key(payload)
    symbol = str(payload.get("token_symbol") or "").upper()
    if not key or not symbol:
        return slots
    balances = dict(slots.get("balances", {}))
    wallet_balances = dict(balances.get(key, {}))
    wallet_balances[symbol] = {
        "balance_formatted": str(payload.get("balance_formatted", "")),
        "token_address": payload.get("token_address"),
    }
    balances[key] = wallet_balances
    slots["balances"] = balances
    return slots


def _write_get_token_price(payload: dict[str, Any], slots: dict[str, Any]) -> dict[str, Any]:
    prices = dict(slots.get("prices", {}))
    by_symbol = dict(prices.get("by_symbol", {}))
    for entry in payload.get("prices", []) or []:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("symbol") or "").upper()
        if not symbol:
            continue
        by_symbol[symbol] = {
            "price": entry.get("price"),
            "market_cap": entry.get("market_cap"),
            "change_24h_pct": entry.get("change_24h_pct"),
        }
    if by_symbol:
        prices["vs_currency"] = payload.get("vs_currency", "usd")
        prices["by_symbol"] = by_symbol
        slots["prices"] = prices
    return slots


def _write_get_defi_positions(payload: dict[str, Any], slots: dict[str, Any]) -> dict[str, Any]:
    key = _wallet_key(payload)
    if not key:
        return slots
    positions = dict(slots.get("defi_positions", {}))
    record: dict[str, Any] = {}
    for protocol in ("aave_v3", "compound_v3", "uniswap_v3"):
        if protocol in payload:
            record[protocol] = payload.get(protocol)
    if payload.get("errors"):
        record["errors"] = payload.get("errors")
    if record:
        positions[key] = record
        slots["defi_positions"] = positions
    return slots


def _write_get_lending_rates(payload: dict[str, Any], slots: dict[str, Any]) -> dict[str, Any]:
    rates = dict(slots.get("rates", {}))
    bucket = {
        "protocol": payload.get("protocol"),
        "chain_id": payload.get("chain_id"),
        "data_block_number": payload.get("data_block_number"),
        "rates": payload.get("rates", []),
        "errors": payload.get("errors", []),
    }
    key = f"{bucket['protocol']}:{bucket['chain_id']}"
    rates[key] = bucket
    slots["rates"] = rates
    return slots


def _write_get_protocol_tvl(payload: dict[str, Any], slots: dict[str, Any]) -> dict[str, Any]:
    protocol = str(payload.get("protocol") or "").strip().lower()
    if not protocol:
        return slots
    tvl = dict(slots.get("tvl", {}))
    tvl[protocol] = {
        "current_tvl_usd": payload.get("current_tvl_usd"),
        "tvl_7d_change_pct": payload.get("tvl_7d_change_pct"),
        "tvl_30d_change_pct": payload.get("tvl_30d_change_pct"),
        "note": payload.get("note"),
    }
    slots["tvl"] = tvl
    return slots


_WRITERS = {
    "get_wallet_portfolio": _write_get_wallet_portfolio,
    "get_erc20_balance": _write_get_erc20_balance,
    "get_token_price": _write_get_token_price,
    "get_defi_positions": _write_get_defi_positions,
    "get_lending_rates": _write_get_lending_rates,
    "get_protocol_tvl": _write_get_protocol_tvl,
}


def summarize_into_slots(tool_name: str, result_content: str, slots: dict[str, Any]) -> dict[str, Any]:
    """Merge a tool result into slots, best-effort and side-effect safe."""
    writer = _WRITERS.get(tool_name)
    if writer is None:
        return slots
    payload = _as_json(result_content)
    if payload is None:
        return slots
    out = dict(slots)
    try:
        return writer(payload, out)
    except Exception:
        return slots


def render_slots_markdown(slots: dict[str, Any]) -> str:
    """Render deterministic markdown facts block for planner context."""
    if not slots:
        return ""

    lines: list[str] = ["## Known Facts (from prior tool calls)"]
    for top_key in sorted(slots.keys()):
        value = slots[top_key]
        if isinstance(value, dict):
            for sub_key in sorted(value.keys()):
                serialized = json.dumps(value[sub_key], sort_keys=True)
                lines.append(f"- {top_key}.{sub_key}: {serialized}")
            continue
        serialized = json.dumps(value, sort_keys=True)
        lines.append(f"- {top_key}: {serialized}")
    return "\n".join(lines)
