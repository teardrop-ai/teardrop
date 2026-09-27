# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Pinned-block on-chain observations and the ``oracle_deviation`` task.

Every read happens at the first block whose timestamp is at or after the target time, and only
once that block is finalized, so any archive node reproduces the stored payload byte for byte.
Raw return data is stored; decoding and scoring are pure functions of the payload.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_abi.exceptions import DecodingError
from web3 import Web3
from web3.exceptions import ContractLogicError

from labeling.contracts import Definition, Observation, ObservationRequest, ScoreResult, TargetDraft, utc_datetime
from tools._internals._web3_helpers import get_web3, rpc_call

logger = logging.getLogger(__name__)

_LATEST_ROUND_DATA = bytes(Web3.keccak(text="latestRoundData()"))[:4]
_SLOT0 = bytes(Web3.keccak(text="slot0()"))[:4]
_OBSERVE = bytes(Web3.keccak(text="observe(uint32[])"))[:4]
_PROBABILITY_TOLERANCE = 1e-6
_TICK_BASE = Decimal("1.0001")


def bucket_labels(bounds: Sequence[Any]) -> list[str]:
    """Return labels covering ``[0, inf)``: ``lt{b0}``, ``b{lo}_{hi}``..., ``gt{bN}``."""
    if (
        not isinstance(bounds, (list, tuple))
        or not bounds
        or any(isinstance(b, bool) or not isinstance(b, int) or b <= 0 for b in bounds)
        or list(bounds) != sorted(set(bounds))
    ):
        raise ValueError("Bucket bounds must be strictly increasing positive integers")
    labels = [f"lt{bounds[0]}"]
    labels.extend(f"b{low}_{high}" for low, high in zip(bounds, bounds[1:]))
    labels.append(f"gt{bounds[-1]}")
    return labels


def bucket_for(value: Decimal, bounds: Sequence[int]) -> str:
    labels = bucket_labels(bounds)
    for label, bound in zip(labels, bounds):
        if value < bound:
            return label
    return labels[-1]


def probabilities(value: Any, labels: Sequence[str]) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(labels):
        raise ValueError("Prediction must assign a probability to every bucket")
    result: dict[str, float] = {}
    for label in labels:
        p = value[label]
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("Bucket probabilities must be numbers between 0 and 1")
        result[label] = float(p)
    if abs(math.fsum(result.values()) - 1.0) > _PROBABILITY_TOLERANCE:
        raise ValueError("Bucket probabilities must sum to 1")
    return result


def brier_result(forecast: Mapping[str, float], outcome: str, actual: dict[str, Any]) -> ScoreResult:
    """Multi-class Brier score (lower is better); unique argmax decides correct/incorrect, ties are neutral."""
    score = math.fsum((p - (1.0 if label == outcome else 0.0)) ** 2 for label, p in forecast.items())
    top = max(forecast.values())
    leaders = [label for label, p in forecast.items() if p == top]
    if len(leaders) > 1:
        status = "neutral"
    else:
        status = "correct" if leaders[0] == outcome else "incorrect"
    return ScoreResult(label=outcome, score=score, status=status, actual=actual)


def round_window(prediction_at: datetime, config: Mapping[str, Any]) -> tuple[datetime, datetime, datetime]:
    """Return ``(start, end, due)`` for the first epoch-aligned round strictly after ``prediction_at``."""
    round_seconds = int(config["round_seconds"])
    horizon_seconds = int(config["horizon_seconds"])
    finality_seconds = int(config["finality_seconds"])
    if round_seconds <= 0 or horizon_seconds <= 0 or finality_seconds < 0:
        raise ValueError("Round configuration is invalid")
    now = math.floor(utc_datetime(prediction_at).timestamp())
    start = datetime.fromtimestamp((now // round_seconds + 1) * round_seconds, tz=timezone.utc)
    end = start + timedelta(seconds=horizon_seconds)
    return start, end, end + timedelta(seconds=finality_seconds)


def parse_oracle_rounds(predictions: dict[str, Any], definition: Definition, prediction_at: datetime) -> Sequence[TargetDraft]:
    labels = bucket_labels(definition.config.get("buckets_bps"))
    forecast = probabilities(predictions.get("deviation_bps"), labels)
    start, end, due = round_window(prediction_at, definition.config)
    return [TargetDraft("root", {"p": forecast}, start, end, due)]


async def _get_block(w3: Any, chain_id: int, identifier: int | str) -> Mapping[str, Any]:
    return await rpc_call(lambda: w3.eth.get_block(identifier), chain_id=chain_id)


async def first_block_at_or_after(w3: Any, chain_id: int, timestamp: int, head: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Binary-search the first block with ``timestamp >= target``; ``None`` until ``head`` reaches it."""
    if timestamp > int(head["timestamp"]):
        return None
    low, high = 0, int(head["number"])
    while low < high:
        middle = (low + high) // 2
        if int((await _get_block(w3, chain_id, middle))["timestamp"]) >= timestamp:
            high = middle
        else:
            low = middle + 1
    return await _get_block(w3, chain_id, low)


async def _call(w3: Any, chain_id: int, to: str, data: bytes, block_number: int) -> str:
    transaction = {"to": Web3.to_checksum_address(to), "data": data}
    try:
        raw = await rpc_call(lambda: w3.eth.call(transaction, block_identifier=block_number), chain_id=chain_id)
    except ContractLogicError:
        # A revert at a finalized block is deterministic, so it is recorded rather than retried.
        return "0x"
    return Web3.to_hex(raw)


async def _snapshot(
    w3: Any,
    config: Mapping[str, Any],
    timestamp: int,
    head: Mapping[str, Any],
    cache: dict[int, dict[str, Any] | None],
) -> dict[str, Any] | None:
    if timestamp in cache:
        return cache[timestamp]
    chain_id = int(config["chain_id"])
    block = await first_block_at_or_after(w3, chain_id, timestamp, head)
    snapshot: dict[str, Any] | None = None
    if block is not None:
        number = int(block["number"])
        observe = _OBSERVE + abi_encode(["uint32[]"], [[int(config["twap_seconds"]), 0]])
        snapshot = {
            "block": number,
            "hash": Web3.to_hex(block["hash"]),
            "timestamp": int(block["timestamp"]),
            "latest_round_data": await _call(w3, chain_id, str(config["feed"]), _LATEST_ROUND_DATA, number),
            "slot0": await _call(w3, chain_id, str(config["pool"]), _SLOT0, number),
            "observe": await _call(w3, chain_id, str(config["pool"]), observe, number),
        }
    cache[timestamp] = snapshot
    return snapshot


def _epoch(value: datetime) -> int:
    return math.floor(utc_datetime(value).timestamp())


class OracleDeviationProvider:
    """Reads Chainlink ``latestRoundData`` and Uniswap v3 ``slot0``/``observe`` at round start and end."""

    def plan(self, target: TargetDraft, definition: Definition) -> ObservationRequest:
        config = definition.config
        return ObservationRequest(
            "oracle_deviation",
            "1",
            {
                "chain_id": int(config["chain_id"]),
                "feed": str(config["feed"]),
                "pool": str(config["pool"]),
                "twap_seconds": int(config["twap_seconds"]),
                "start": target.window_start.isoformat(),
            },
            target.window_end,
        )

    async def fetch_batch(
        self,
        requests: Sequence[ObservationRequest],
        definition: Definition,
    ) -> Mapping[str, Observation]:
        config = definition.config
        chain_id = int(config["chain_id"])
        try:
            w3 = get_web3(chain_id)
            head = await _get_block(w3, chain_id, "finalized")
        except Exception as exc:
            # RPC URLs may embed credentials, so only the exception type is logged.
            logger.warning("oracle finalized head unavailable error=%s", type(exc).__name__)
            return {}
        cache: dict[int, dict[str, Any] | None] = {}
        observations: dict[str, Observation] = {}
        for request in requests:
            try:
                start = await _snapshot(w3, config, _epoch(datetime.fromisoformat(request.request["start"])), head, cache)
                end = await _snapshot(w3, config, _epoch(request.as_of), head, cache)
            except Exception as exc:
                logger.warning("oracle observation failed request=%s error=%s", request.request_sha256[:12], type(exc).__name__)
                continue
            if start is not None and end is not None:
                observations[request.request_sha256] = Observation(request=request, payload={"start": start, "end": end})
        return observations


def _return_data(value: Any, words: int) -> bytes:
    if not isinstance(value, str):
        raise ValueError("Return data is missing")
    raw = bytes.fromhex(value.removeprefix("0x"))
    if len(raw) < 32 * words:
        raise ValueError("Return data is short")
    return raw


def _deviation(snapshot: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[Decimal, dict[str, Any]]:
    feed = _return_data(snapshot["latest_round_data"], 5)
    round_id, answer, _, updated_at, answered_in_round = abi_decode(
        ["uint80", "int256", "uint256", "uint256", "uint80"], feed[:160]
    )
    age = int(snapshot["timestamp"]) - int(updated_at)
    if round_id <= 0 or answered_in_round < round_id or answer <= 0 or not 0 <= age <= int(config["max_stale_seconds"]):
        raise ValueError("Oracle answer is stale or invalid")
    _return_data(snapshot["slot0"], 7)
    tick_cumulatives, _ = abi_decode(["int56[]", "uint160[]"], _return_data(snapshot["observe"], 2))
    if len(tick_cumulatives) != 2:
        raise ValueError("TWAP observation is incomplete")
    # Floor division matches Uniswap OracleLibrary rounding toward negative infinity.
    mean_tick = (int(tick_cumulatives[1]) - int(tick_cumulatives[0])) // int(config["twap_seconds"])
    if not -887272 <= mean_tick <= 887272:
        raise ValueError("TWAP tick is outside the Uniswap range")
    with localcontext() as ctx:
        ctx.prec = 60
        oracle = Decimal(answer).scaleb(-int(config["feed_decimals"]))
        twap = (_TICK_BASE**mean_tick).scaleb(int(config["token0_decimals"]) - int(config["token1_decimals"]))
        deviation = abs(oracle - twap) / twap * 10000
    evidence = {
        "block": int(snapshot["block"]),
        "round_id": str(round_id),
        "updated_at": int(updated_at),
        "answer": str(answer),
        "mean_tick": mean_tick,
    }
    return deviation, evidence


def _fixed(value: Decimal) -> str:
    with localcontext() as ctx:
        ctx.prec = 60
        return format(value.quantize(Decimal("0.0001")), "f")


def score_oracle_deviation(target: dict[str, Any], observation: Observation | None, definition: Definition) -> ScoreResult:
    config = definition.config
    try:
        bounds = config["buckets_bps"]
        forecast = probabilities(target.get("p"), bucket_labels(bounds))
        payload = observation.payload if observation is not None else None
        if not isinstance(payload, dict):
            raise ValueError("Observation payload is missing")
        start_deviation, start = _deviation(payload["start"], config)
        end_deviation, end = _deviation(payload["end"], config)
        outcome = bucket_for(end_deviation, bounds)
        actual = {
            "bucket": outcome,
            "deviation_bps": _fixed(end_deviation),
            "start_deviation_bps": _fixed(start_deviation),
            "start": start,
            "end": end,
        }
    except (ArithmeticError, DecodingError, KeyError, TypeError, ValueError):
        return ScoreResult(
            label="unavailable",
            status="unavailable",
            rationale="Oracle observation is missing, short, or stale.",
        )
    return brier_result(forecast, outcome, actual)
