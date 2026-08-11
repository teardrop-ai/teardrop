# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""get_transaction – fetch transaction details + receipt by hash."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field
from web3 import Web3

from tools._internals._web3_helpers import get_web3
from tools.registry import ToolDefinition

_CALLDATA_MAX_CHARS = 16_386  # 8 KiB of calldata plus the 0x prefix.
_LOG_DATA_MAX_CHARS = 8_194  # 4 KiB of log data plus the 0x prefix.
_MAX_LOGS = 50
_MAX_LOG_TOPICS = 4


def _to_hex(value: Any) -> str:
    """Convert Web3 bytes-like values to a stable, JSON-safe hex string."""
    if value is None:
        return "0x"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"0x{bytes(value).hex()}"
    if isinstance(value, str):
        encoded = value
    else:
        try:
            encoded = value.hex()
        except (AttributeError, TypeError, ValueError):
            return "0x"
    if isinstance(encoded, str):
        encoded = encoded if encoded[:2].lower() == "0x" else f"0x{encoded}"
        try:
            bytes.fromhex(encoded[2:])
        except ValueError:
            return "0x"
        return encoded
    return "0x"


def _truncate_hex(value: str, max_chars: int) -> tuple[str, bool]:
    """Bound hex payloads while preserving the ``0x`` prefix and byte pairs."""
    if len(value) <= max_chars:
        return value, False
    limit = max_chars if max_chars % 2 == 0 else max_chars - 1
    return value[:limit], True


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return int(value, 0) if value.lower().startswith("0x") else int(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw_value = bytes(value)
        if not raw_value:
            return None
        try:
            return int.from_bytes(raw_value, byteorder="big")
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_logs(receipt: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], bool]:
    """Normalize and bound receipt logs for agent-friendly output."""
    raw_logs = receipt.get("logs", []) if receipt else []
    if not isinstance(raw_logs, (list, tuple)):
        return [], False

    logs: list[dict[str, Any]] = []
    for raw_log in raw_logs[:_MAX_LOGS]:
        if not isinstance(raw_log, Mapping):
            continue
        raw_data = raw_log.get("data")
        data, data_truncated = _truncate_hex(
            _to_hex(raw_data) if raw_data is not None else "0x",
            _LOG_DATA_MAX_CHARS,
        )
        raw_topics = raw_log.get("topics") or []
        if not isinstance(raw_topics, (list, tuple)):
            raw_topics = []
        raw_address = raw_log.get("address")
        logs.append(
            {
                "address": _to_hex(raw_address) if raw_address else "",
                "topics": [_to_hex(topic) for topic in raw_topics[:_MAX_LOG_TOPICS]],
                "topics_truncated": len(raw_topics) > _MAX_LOG_TOPICS,
                "data": data,
                "data_truncated": data_truncated,
                "log_index": _optional_int(raw_log.get("logIndex")),
            }
        )
    return logs, len(raw_logs) > _MAX_LOGS


# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetTransactionInput(BaseModel):
    tx_hash: str = Field(..., description="Transaction hash (0x…)")
    chain_id: int = Field(default=1, description="Chain ID (1=Ethereum, 8453=Base)")


class TransactionLog(BaseModel):
    address: str
    topics: list[str]
    topics_truncated: bool = False
    data: str
    data_truncated: bool = False
    log_index: int | None = None


class GetTransactionOutput(BaseModel):
    tx_hash: str
    from_address: str
    to_address: str | None
    value_eth: str
    gas_used: int | None
    gas_price_gwei: str | None
    status: int | None = Field(None, description="1=success, 0=revert, None=pending")
    block_number: int | None
    chain_id: int
    input_data: str | None = Field(None, description="Hex calldata, bounded to 8 KiB")
    input_data_truncated: bool = False
    transaction_index: int | None = None
    effective_gas_price_gwei: str | None = None
    fee_wei: str | None = None
    fee_eth: str | None = None
    logs: list[TransactionLog] = Field(default_factory=list)
    logs_truncated: bool = False


# ─── Implementation ──────────────────────────────────────────────────────────


async def get_transaction(tx_hash: str, chain_id: int = 1) -> dict[str, Any]:
    """Return transaction details and receipt status."""
    w3 = get_web3(chain_id)

    tx = await w3.eth.get_transaction(tx_hash)

    # Try to get receipt (may fail for pending txns)
    receipt = None
    try:
        receipt = await w3.eth.get_transaction_receipt(tx_hash)
    except Exception:
        pass

    value_wei = _optional_int(tx.get("value")) or 0
    value_eth = str(Web3.from_wei(value_wei, "ether"))
    gas_price = _optional_int(tx.get("gasPrice"))
    gas_price_gwei = str(Web3.from_wei(gas_price, "gwei")) if gas_price is not None else None

    input_data: str | None = None
    input_data_truncated = False
    raw_input = tx.get("input")
    if raw_input is not None:
        input_data, input_data_truncated = _truncate_hex(_to_hex(raw_input), _CALLDATA_MAX_CHARS)

    logs, logs_truncated = _normalize_logs(receipt)
    transaction_index_value = receipt.get("transactionIndex") if receipt else None
    if transaction_index_value is None:
        transaction_index_value = tx.get("transactionIndex")
    transaction_index = _optional_int(transaction_index_value)
    effective_gas_price = _optional_int(receipt.get("effectiveGasPrice")) if receipt else None
    effective_gas_price_gwei = str(Web3.from_wei(effective_gas_price, "gwei")) if effective_gas_price is not None else None
    gas_used = _optional_int(receipt.get("gasUsed")) if receipt else None
    fee_wei = None
    fee_eth = None
    if gas_used is not None and effective_gas_price is not None and gas_used >= 0 and effective_gas_price >= 0:
        fee_wei_value = gas_used * effective_gas_price
        fee_wei = str(fee_wei_value)
        fee_eth = str(Web3.from_wei(fee_wei_value, "ether"))

    return {
        "tx_hash": tx_hash,
        "from_address": tx.get("from", ""),
        "to_address": tx.get("to"),
        "value_eth": value_eth,
        "gas_used": gas_used,
        "gas_price_gwei": gas_price_gwei,
        "status": _optional_int(receipt.get("status")) if receipt else None,
        "block_number": _optional_int(tx.get("blockNumber")),
        "chain_id": chain_id,
        "input_data": input_data,
        "input_data_truncated": input_data_truncated,
        "transaction_index": transaction_index,
        "effective_gas_price_gwei": effective_gas_price_gwei,
        "fee_wei": fee_wei,
        "fee_eth": fee_eth,
        "logs": logs,
        "logs_truncated": logs_truncated,
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_transaction",
    version="1.1.0",
    description=(
        "Get details and receipt for an Ethereum or Base transaction by hash. "
        "Includes bounded calldata, normalized event logs, transaction index, and "
        "receipt-derived effective gas price and fee when available."
    ),
    tags=["web3", "ethereum", "transaction"],
    input_schema=GetTransactionInput,
    output_schema=GetTransactionOutput,
    show_on_agent_card=False,
    implementation=get_transaction,
)
