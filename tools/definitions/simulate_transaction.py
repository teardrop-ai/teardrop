# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""simulate_transaction – dry-run a transaction or ordered bundle without signing or broadcasting.

Uses ``eth_simulateV1`` so later calls in a bundle see earlier calls' state (e.g. approve then
swap), with ``traceTransfers`` exposing native ETH movements as ERC-20-style Transfer logs.
When the RPC endpoint does not support ``eth_simulateV1``, each call runs independently via
``eth_call`` + ``eth_estimateGas`` pinned to the same block and the result is marked degraded.

Every result carries a leaf-v2 execution-receipt ``content_sha256`` committing to the inputs
and outcomes (see ``labeling.commitments``); persistence and anchoring are handled elsewhere.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, Literal

from eth_abi import decode as abi_decode
from pydantic import BaseModel, Field, field_validator, model_validator
from web3 import Web3

from labeling.commitments import LEAF_VERSION_EXECUTION_RECEIPT, receipt_content_sha256
from tools._internals._web3_helpers import get_web3, rpc_call
from tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

SUPPORTED_CHAIN_IDS = frozenset({1, 8453})
MAX_CALLS = 8
_MAX_CALLDATA_HEX_CHARS = 2 + 2 * 49_152
_MAX_ABI_LEN = 65_536
_MAX_RETURN_HEX_CHARS = 2 + 2 * 1_024
_MAX_REASON_CHARS = 500
_MAX_REPORTED_CHANGES = 50
_MAX_UINT256 = 2**256 - 1
_UNLIMITED_APPROVAL_THRESHOLD = 2**128

_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_APPROVAL_TOPIC = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
# eth_simulateV1 traceTransfers reports native ETH movements as Transfer logs from this address.
_NATIVE_TRANSFER_EMITTER = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
_ERROR_SELECTOR = "08c379a0"
_PANIC_SELECTOR = "4e487b71"
_METHOD_NOT_FOUND_CODE = -32601
_METHOD_UNSUPPORTED_MARKERS = (
    "method not found",
    "unsupported method",
    "not supported",
    "does not exist",
    "is not available",
    "not whitelisted",
)
_HEX_DATA_RE = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")
_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_FALLBACK_WARNING = (
    "Bundle simulation is unavailable on this RPC endpoint: calls ran independently against the same block, "
    "so later calls did not see earlier calls' state changes and balance/approval changes are not reported."
)
_GAS_FEE_WARNING = "Gas fees, nonce, and signature are not checked; ETH balance changes exclude gas costs."


def _checksum(value: str, field: str) -> str:
    try:
        return Web3.to_checksum_address(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid EVM address") from exc


# ─── Schemas ──────────────────────────────────────────────────────────────────


class SimulationCall(BaseModel):
    to: str = Field(..., description="Target address.")
    data: str | None = Field(
        default=None,
        description="Hex calldata; or use abi_fragment + function_name.",
        max_length=_MAX_CALLDATA_HEX_CHARS,
    )
    abi_fragment: str | None = Field(
        default=None,
        description="JSON ABI array used to encode function_name.",
        max_length=_MAX_ABI_LEN,
    )
    function_name: str | None = Field(default=None, max_length=200)
    args_json: str | None = Field(
        default=None,
        description="JSON array of positional args; integers may be strings.",
        max_length=4_000,
    )
    value_wei: str = Field(default="0", description="ETH value in wei (decimal string).", max_length=78)

    @field_validator("to")
    @classmethod
    def _validate_to(cls, value: str) -> str:
        return _checksum(value, "to")

    @field_validator("data")
    @classmethod
    def _validate_data(cls, value: str | None) -> str | None:
        if value is not None and not _HEX_DATA_RE.fullmatch(value):
            raise ValueError("data must be 0x-prefixed hex with an even number of digits")
        return value.lower() if value is not None else None

    @field_validator("value_wei")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        if not value.isdigit() or int(value) > _MAX_UINT256:
            raise ValueError("value_wei must be a non-negative decimal integer within uint256")
        return str(int(value))

    @model_validator(mode="after")
    def _exactly_one_calldata_source(self) -> SimulationCall:
        uses_abi = self.abi_fragment is not None or self.function_name is not None or self.args_json is not None
        if self.data is not None and uses_abi:
            raise ValueError("Provide either data or abi_fragment + function_name, not both")
        if uses_abi and not (self.abi_fragment and self.function_name):
            raise ValueError("abi_fragment and function_name are both required to encode calldata")
        return self


class SimulateTransactionInput(BaseModel):
    from_address: str = Field(..., description="Sender wallet (msg.sender).")
    calls: list[SimulationCall] = Field(
        ...,
        description="Ordered bundle, e.g. [approve, swap]; later calls see earlier state.",
        min_length=1,
        max_length=MAX_CALLS,
    )
    chain_id: int = Field(default=1, description="1=Ethereum, 8453=Base.")
    block_identifier: str = Field(
        default="latest",
        description="'latest' or a decimal parent block number.",
        max_length=20,
    )

    @field_validator("from_address")
    @classmethod
    def _validate_from(cls, value: str) -> str:
        return _checksum(value, "from_address")

    @field_validator("chain_id")
    @classmethod
    def _validate_chain(cls, value: int) -> int:
        if value not in SUPPORTED_CHAIN_IDS:
            raise ValueError(f"Unsupported chain_id={value}; supported: {sorted(SUPPORTED_CHAIN_IDS)}")
        return value

    @field_validator("block_identifier")
    @classmethod
    def _validate_block(cls, value: str) -> str:
        if value != "latest" and not value.isdigit():
            raise ValueError("block_identifier must be 'latest' or a decimal block number")
        return value


class CallOutcome(BaseModel):
    index: int
    to: str
    status: Literal["success", "reverted", "failed"]
    gas_used: int | None = None
    return_data: str | None = None
    return_data_truncated: bool = False
    revert_reason: str | None = None
    error: str | None = None


class BalanceChange(BaseModel):
    token: str
    delta_raw: str


class ApprovalChange(BaseModel):
    token: str
    spender: str
    amount_raw: str
    unlimited: bool


class SimulationReceipt(BaseModel):
    version: int
    kind: Literal["simulation"]
    content_sha256: str


class SimulateTransactionOutput(BaseModel):
    chain_id: int
    from_address: str
    mode: Literal["eth_simulateV1", "eth_call_fallback"]
    base_block_number: int
    all_succeeded: bool
    calls: list[CallOutcome]
    balance_changes: list[BalanceChange]
    approvals: list[ApprovalChange]
    warnings: list[str]
    receipt: SimulationReceipt


# ─── RPC helpers ──────────────────────────────────────────────────────────────


class _RpcError(Exception):
    def __init__(self, code: Any, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


async def _rpc_request(w3: Any, chain_id: int, method: str, params: list[Any]) -> Any:
    response = await rpc_call(lambda: w3.provider.make_request(method, params), chain_id=chain_id)
    if not isinstance(response, dict):
        raise _RpcError(None, f"Malformed {method} response")
    error = response.get("error")
    if error:
        if isinstance(error, dict):
            raise _RpcError(error.get("code"), str(error.get("message") or ""), error.get("data"))
        raise _RpcError(None, str(error))
    return response.get("result")


def _is_method_unsupported(exc: Exception) -> bool:
    if isinstance(exc, _RpcError) and exc.code == _METHOD_NOT_FOUND_CODE:
        return True
    text = str(exc).lower()
    if "method not found" in text or "unsupported method" in text:
        return True
    return "eth_simulatev1" in text and any(marker in text for marker in _METHOD_UNSUPPORTED_MARKERS)


def _parse_quantity(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None


# ─── Decoding ─────────────────────────────────────────────────────────────────


_INTEGER_STRING_RE = re.compile(r"^-?\d{1,78}$")


def _coerce_integer_args(abi: list[Any], function_name: str, args: list[Any]) -> list[Any]:
    """Convert decimal-string arguments to ints for top-level (u)int parameters; LLMs often quote amounts."""
    for entry in abi:
        if not isinstance(entry, dict) or entry.get("name") != function_name:
            continue
        inputs = entry.get("inputs") or []
        if len(inputs) != len(args):
            continue
        coerced = list(args)
        for position, (param, arg) in enumerate(zip(inputs, args, strict=True)):
            param_type = str(param.get("type", "")) if isinstance(param, dict) else ""
            if (
                param_type.startswith(("uint", "int"))
                and "[" not in param_type
                and isinstance(arg, str)
                and _INTEGER_STRING_RE.fullmatch(arg)
            ):
                coerced[position] = int(arg)
        return coerced
    return args


def _encode_calldata(call: SimulationCall) -> str:
    if call.data is not None:
        return call.data
    try:
        abi = json.loads(call.abi_fragment or "")
        args = json.loads(call.args_json) if call.args_json else []
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in abi_fragment or args_json: {exc.msg}") from exc
    if not isinstance(abi, list) or not isinstance(args, list):
        raise ValueError("abi_fragment and args_json must be JSON arrays")
    function_name = call.function_name or ""
    args = _coerce_integer_args(abi, function_name, args)
    try:
        encoded = Web3().eth.contract(abi=abi).encode_abi(function_name, args=args)
    except Exception as exc:
        raise ValueError(
            f"Could not ABI-encode {function_name} with {len(args)} argument(s); check the ABI and argument types"
        ) from exc
    return str(encoded).lower()


def decode_revert_reason(data: Any) -> str | None:
    """Decode ``Error(string)`` / ``Panic(uint256)`` revert data; name other selectors as custom errors."""
    if not isinstance(data, str) or not _HEX_DATA_RE.fullmatch(data) or len(data) < 10:
        return None
    selector = data[2:10].lower()
    try:
        payload = bytes.fromhex(data[10:])
        if selector == _ERROR_SELECTOR:
            (reason,) = abi_decode(["string"], payload)
            return str(reason)[:_MAX_REASON_CHARS]
        if selector == _PANIC_SELECTOR:
            (code,) = abi_decode(["uint256"], payload)
            return f"Panic(0x{code:02x})"
    except Exception:
        logger.debug("Undecodable revert payload for selector %s", selector)
    return f"custom error 0x{selector}"


def _topic_address(topic: str) -> str:
    return Web3.to_checksum_address("0x" + topic[-40:])


def _summarize_logs(logs: list[dict[str, Any]], owner: str) -> tuple[list[BalanceChange], list[ApprovalChange]]:
    deltas: dict[str, int] = {}
    approvals: dict[tuple[str, str], int] = {}
    for log in logs:
        topics = [str(topic).lower() for topic in (log.get("topics") or [])]
        # ERC-721 Transfer/Approval index tokenId as a 4th topic; only fungible 3-topic events count.
        if len(topics) != 3 or topics[0] not in (_TRANSFER_TOPIC, _APPROVAL_TOPIC):
            continue
        data = log.get("data")
        emitter = str(log.get("address") or "").lower()
        if not (isinstance(data, str) and len(data) == 66 and all(len(topic) == 66 for topic in topics)):
            continue
        amount = _parse_quantity(data)
        if amount is None or not _ADDRESS_RE.fullmatch(emitter):
            continue
        first, second = _topic_address(topics[1]), _topic_address(topics[2])
        if topics[0] == _TRANSFER_TOPIC:
            token = "ETH" if emitter == _NATIVE_TRANSFER_EMITTER else Web3.to_checksum_address(emitter)
            if first == owner:
                deltas[token] = deltas.get(token, 0) - amount
            if second == owner:
                deltas[token] = deltas.get(token, 0) + amount
        elif first == owner:
            approvals[(Web3.to_checksum_address(emitter), second)] = amount
    balance_changes = [BalanceChange(token=token, delta_raw=str(delta)) for token, delta in sorted(deltas.items()) if delta]
    approval_changes = [
        ApprovalChange(token=token, spender=spender, amount_raw=str(amount), unlimited=amount >= _UNLIMITED_APPROVAL_THRESHOLD)
        for (token, spender), amount in sorted(approvals.items())
    ]
    return balance_changes, approval_changes


def _success_outcome(index: int, to: str, gas_used: int | None, raw_return: Any) -> tuple[CallOutcome, str | None]:
    """Return the bounded outcome plus the full return data (hashed into the receipt)."""
    full = raw_return if isinstance(raw_return, str) and _HEX_DATA_RE.fullmatch(raw_return) else None
    truncated = full is not None and len(full) > _MAX_RETURN_HEX_CHARS
    outcome = CallOutcome(
        index=index,
        to=to,
        status="success",
        gas_used=gas_used,
        return_data=full[:_MAX_RETURN_HEX_CHARS] if full is not None else None,
        return_data_truncated=truncated,
    )
    return outcome, full


def _outcome_from_error(index: int, to: str, exc: _RpcError, gas_used: int | None = None) -> tuple[CallOutcome, str | None]:
    reason = decode_revert_reason(exc.data)
    reverted = exc.code == 3 or reason is not None
    outcome = CallOutcome(
        index=index,
        to=to,
        status="reverted" if reverted else "failed",
        gas_used=gas_used,
        revert_reason=reason,
        error=exc.message[:_MAX_REASON_CHARS] or None,
    )
    return outcome, None


# ─── Execution modes ──────────────────────────────────────────────────────────

_Outcomes = list[tuple[CallOutcome, str | None]]


async def _simulate_bundle(
    w3: Any, request: SimulateTransactionInput, encoded: list[str], block_hex: str
) -> tuple[_Outcomes, list[dict[str, Any]]]:
    call_objects = []
    for call, data in zip(request.calls, encoded, strict=True):
        obj: dict[str, Any] = {"from": request.from_address, "to": call.to, "input": data}
        if int(call.value_wei):
            obj["value"] = hex(int(call.value_wei))
        call_objects.append(obj)
    payload = {"blockStateCalls": [{"calls": call_objects}], "traceTransfers": True, "validation": False}
    blocks = await _rpc_request(w3, request.chain_id, "eth_simulateV1", [payload, block_hex])
    if not isinstance(blocks, list) or not blocks or not isinstance(blocks[0], dict):
        raise ValueError("Malformed eth_simulateV1 response")
    results = blocks[0].get("calls")
    if not isinstance(results, list) or len(results) != len(request.calls):
        raise ValueError("eth_simulateV1 returned a different number of call results than requested")

    outcomes: _Outcomes = []
    logs: list[dict[str, Any]] = []
    for index, (call, result) in enumerate(zip(request.calls, results, strict=True)):
        if not isinstance(result, dict):
            raise ValueError("Malformed eth_simulateV1 call result")
        gas_used = _parse_quantity(result.get("gasUsed"))
        error = result.get("error")
        if result.get("status") == "0x1" and not error:
            outcomes.append(_success_outcome(index, call.to, gas_used, result.get("returnData")))
            logs.extend(log for log in (result.get("logs") or []) if isinstance(log, dict))
            continue
        error = error if isinstance(error, dict) else {}
        rpc_error = _RpcError(error.get("code"), str(error.get("message") or ""), error.get("data"))
        outcomes.append(_outcome_from_error(index, call.to, rpc_error, gas_used))
    return outcomes, logs


async def _simulate_independently(w3: Any, request: SimulateTransactionInput, encoded: list[str], block_hex: str) -> _Outcomes:
    async def run_one(index: int, call: SimulationCall, data: str) -> tuple[CallOutcome, str | None]:
        tx: dict[str, Any] = {"from": request.from_address, "to": call.to, "data": data}
        if int(call.value_wei):
            tx["value"] = hex(int(call.value_wei))
        try:
            raw = await _rpc_request(w3, request.chain_id, "eth_call", [tx, block_hex])
        except _RpcError as exc:
            return _outcome_from_error(index, call.to, exc)
        try:
            gas_used = _parse_quantity(await _rpc_request(w3, request.chain_id, "eth_estimateGas", [tx, block_hex]))
        except _RpcError:
            gas_used = None
        return _success_outcome(index, call.to, gas_used, raw)

    calls = enumerate(zip(request.calls, encoded, strict=True))
    return list(await asyncio.gather(*(run_one(index, call, data) for index, (call, data) in calls)))


def _sha256_hex_data(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(bytes.fromhex(value[2:])).hexdigest()


def _receipt_content(
    request: SimulateTransactionInput,
    encoded: list[str],
    base_block: int,
    mode: str,
    outcomes: _Outcomes,
    balance_changes: list[BalanceChange],
    approvals: list[ApprovalChange],
) -> dict[str, Any]:
    # Calldata and return data are hashed so receipt content stays bounded regardless of payload size.
    return {
        "producer": "simulate_transaction",
        "chain_id": request.chain_id,
        "from": request.from_address,
        "base_block_number": base_block,
        "mode": mode,
        "calls": [
            {"to": call.to, "value_wei": call.value_wei, "data_sha256": _sha256_hex_data(data)}
            for call, data in zip(request.calls, encoded, strict=True)
        ],
        "results": [
            {
                "status": outcome.status,
                "gas_used": outcome.gas_used,
                "return_data_sha256": _sha256_hex_data(full),
                "revert_reason": outcome.revert_reason,
            }
            for outcome, full in outcomes
        ],
        "balance_changes": [change.model_dump() for change in balance_changes],
        "approvals": [approval.model_dump() for approval in approvals],
    }


# ─── Implementation ──────────────────────────────────────────────────────────


async def simulate_transaction(
    from_address: str,
    calls: list[Any],
    chain_id: int = 1,
    block_identifier: str = "latest",
) -> dict[str, Any]:
    """Simulate an ordered bundle of calls from ``from_address`` without signing or broadcasting."""
    request = SimulateTransactionInput.model_validate(
        {
            "from_address": from_address,
            "calls": [call.model_dump() if isinstance(call, BaseModel) else call for call in calls],
            "chain_id": chain_id,
            "block_identifier": block_identifier,
        }
    )
    encoded = [_encode_calldata(call) for call in request.calls]
    w3 = get_web3(request.chain_id)

    if request.block_identifier == "latest":
        base_block = _parse_quantity(await _rpc_request(w3, request.chain_id, "eth_blockNumber", []))
        if base_block is None:
            raise ValueError("Malformed eth_blockNumber response")
    else:
        base_block = int(request.block_identifier)
    block_hex = hex(base_block)

    mode: Literal["eth_simulateV1", "eth_call_fallback"] = "eth_simulateV1"
    raw_logs: list[dict[str, Any]] = []
    warnings = [_GAS_FEE_WARNING]
    try:
        outcomes, raw_logs = await _simulate_bundle(w3, request, encoded, block_hex)
    except (ValueError, asyncio.TimeoutError):
        raise
    except Exception as exc:
        if not _is_method_unsupported(exc):
            if isinstance(exc, _RpcError):
                raise ValueError(f"eth_simulateV1 failed: {exc.message[:_MAX_REASON_CHARS]}") from exc
            raise
        logger.info("eth_simulateV1 unsupported on chain %d; using eth_call fallback", request.chain_id)
        mode = "eth_call_fallback"
        outcomes = await _simulate_independently(w3, request, encoded, block_hex)
        warnings = [_FALLBACK_WARNING, _GAS_FEE_WARNING]

    balance_changes, approvals = _summarize_logs(raw_logs, request.from_address)
    if len(balance_changes) > _MAX_REPORTED_CHANGES or len(approvals) > _MAX_REPORTED_CHANGES:
        balance_changes, approvals = balance_changes[:_MAX_REPORTED_CHANGES], approvals[:_MAX_REPORTED_CHANGES]
        warnings.append(f"Balance or approval changes exceeded {_MAX_REPORTED_CHANGES} entries; the list is truncated.")
    content = _receipt_content(request, encoded, base_block, mode, outcomes, balance_changes, approvals)
    call_outcomes = [outcome for outcome, _ in outcomes]

    return SimulateTransactionOutput(
        chain_id=request.chain_id,
        from_address=request.from_address,
        mode=mode,
        base_block_number=base_block,
        all_succeeded=all(outcome.status == "success" for outcome in call_outcomes),
        calls=call_outcomes,
        balance_changes=balance_changes,
        approvals=approvals,
        warnings=warnings,
        receipt=SimulationReceipt(
            version=LEAF_VERSION_EXECUTION_RECEIPT,
            kind="simulation",
            content_sha256=receipt_content_sha256(content),
        ),
    ).model_dump()


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="simulate_transaction",
    version="1.0.0",
    description=(
        "Dry-run one transaction or an ordered bundle (e.g. approve then swap) from a wallet on Ethereum or Base "
        "without signing or broadcasting. Returns per-call success or decoded revert reason, gas used, the wallet's "
        "net ETH and ERC-20 balance changes, new token approvals (flagging unlimited ones), and a receipt hash "
        "committing to the inputs and results at the simulated block. Use the result to decide whether to proceed, "
        "adjust, or abort before acting on-chain."
    ),
    tags=["web3", "ethereum", "base", "simulation", "transaction", "defi", "risk"],
    use_when=(
        "Use before recommending or preparing any state-changing transaction to confirm it will not revert and to see "
        "actual token outflows, inflows, and approvals. Put dependent steps (approve then swap) in one call so later "
        "steps see earlier state. Use read_contract for view-only reads and decode_transaction for already-mined txs."
    ),
    limitations=(
        "Point-in-time result at the reported block; mempool ordering, MEV, and later state can change the real "
        "outcome. Gas affordability, nonce, and signatures are not checked. Max 8 calls; Ethereum and Base only. If "
        "bundle simulation is unavailable, calls run independently and balance/approval changes are omitted."
    ),
    alternatives=["read_contract", "decode_transaction", "get_dex_quote", "get_token_approvals"],
    input_schema=SimulateTransactionInput,
    output_schema=SimulateTransactionOutput,
    annotations={"readOnlyHint": True},
    show_on_agent_card=False,
    implementation=simulate_transaction,
)
