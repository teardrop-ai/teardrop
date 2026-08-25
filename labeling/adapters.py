# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from labeling.contracts import Definition, Observation, ObservationRequest, ScoreResult, TargetDraft, utc_datetime
from labeling.registry import register_parser, register_provider, register_scorer
from tools.definitions.get_protocol_tvl import get_protocol_tvl
from tools.definitions.get_token_price import get_token_price
from tools.definitions.get_yield_rates import get_yield_rates

_DECENTRALIZED = ("LUSD", "BOLD", "crvUSD", "GHO", "DAI")
_CENTRALIZED = ("USDT", "USDC", "PYUSD", "RLUSD", "USDG", "USD1", "FDUSD", "TUSD", "USDP", "USDS")


def _horizon(definition: Definition) -> timedelta:
    seconds = int(definition.config.get("horizon_seconds", 604800))
    if seconds <= 0:
        raise ValueError("Definition horizon must be positive")
    return timedelta(seconds=seconds)


def _targets_from_list(
    predictions: dict[str, Any],
    definition: Definition,
    prediction_at: datetime,
    field: str,
    key: str,
) -> list[TargetDraft]:
    values = predictions.get(field)
    if not isinstance(values, list) or not values:
        raise ValueError("Prediction target collection is missing")
    start = utc_datetime(prediction_at)
    end = start + _horizon(definition)
    targets: list[TargetDraft] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError("Prediction target item is invalid")
        item_key = value[key].strip()
        if item_key in seen:
            raise ValueError("Prediction target keys must be unique")
        seen.add(item_key)
        targets.append(TargetDraft(item_key, value, start, end, end))
    return targets


def parse_entry_timing(predictions: dict[str, Any], definition: Definition, prediction_at: datetime) -> Sequence[TargetDraft]:
    return _targets_from_list(predictions, definition, prediction_at, "tokens", "id")


def parse_eth_protocols(predictions: dict[str, Any], definition: Definition, prediction_at: datetime) -> Sequence[TargetDraft]:
    return _targets_from_list(predictions, definition, prediction_at, "protocols", "slug")


def parse_stablecoin_root(predictions: dict[str, Any], definition: Definition, prediction_at: datetime) -> Sequence[TargetDraft]:
    start = utc_datetime(prediction_at)
    end = start + _horizon(definition)
    return [TargetDraft("root", predictions, start, end, end)]


class TokenPriceProvider:
    def plan(self, target: TargetDraft, definition: Definition) -> ObservationRequest:
        token = target.item_payload.get("id")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Token target is missing an identifier")
        return ObservationRequest("token_price", "1", {"token": token.strip()}, target.window_end)

    async def fetch_batch(
        self,
        requests: Sequence[ObservationRequest],
        definition: Definition,
    ) -> Mapping[str, Observation]:
        tokens = list(dict.fromkeys(str(request.request["token"]) for request in requests))
        response = await get_token_price(tokens=tokens, vs_currency="usd")
        entries = {str(item.get("id")): item for item in response.get("prices", []) if isinstance(item, dict)}
        return {
            request.request_sha256: Observation(
                request=request,
                payload=entries.get(str(request.request["token"])),
                status="ready",
            )
            for request in requests
        }


class ProtocolFeesProvider:
    def plan(self, target: TargetDraft, definition: Definition) -> ObservationRequest:
        slug = target.item_payload.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("Protocol target is missing a slug")
        return ObservationRequest("protocol_fees", "1", {"protocol": slug.strip()}, target.window_end)

    async def fetch_batch(
        self,
        requests: Sequence[ObservationRequest],
        definition: Definition,
    ) -> Mapping[str, Observation]:
        protocols = list(dict.fromkeys(str(request.request["protocol"]) for request in requests))
        response = await get_protocol_tvl(protocols=protocols, include_historical=True, days=30)
        entries = (
            {str(item.get("protocol")): item for item in response if isinstance(item, dict) and item.get("protocol") is not None}
            if isinstance(response, list)
            else {}
        )
        return {
            request.request_sha256: Observation(
                request=request,
                payload=entries.get(str(request.request["protocol"])),
                status="ready",
            )
            for request in requests
        }


class StablecoinMarketProvider:
    def plan(self, target: TargetDraft, definition: Definition) -> ObservationRequest:
        return ObservationRequest(
            "stablecoin_market",
            "1",
            {
                "symbols": list(_DECENTRALIZED + _CENTRALIZED),
                "min_tvl_usd": 1_000_000,
                "max_apy": 30,
            },
            target.window_end,
        )

    async def fetch_batch(
        self,
        requests: Sequence[ObservationRequest],
        definition: Definition,
    ) -> Mapping[str, Observation]:
        symbols = list(_DECENTRALIZED + _CENTRALIZED)
        yields, prices = await __import__("asyncio").gather(
            get_yield_rates(
                stable_only=True,
                max_apy=30,
                min_tvl_usd=1_000_000,
                limit=50,
                symbols_any=symbols,
            ),
            get_token_price(tokens=symbols, vs_currency="usd"),
        )
        payload = {"yield": yields, "prices": prices}
        return {request.request_sha256: Observation(request=request, payload=payload, status="ready") for request in requests}


def _scored(label: str, correct: bool, actual: dict[str, Any]) -> ScoreResult:
    return ScoreResult(
        label=label,
        score=1.0 if correct else -1.0,
        status="correct" if correct else "incorrect",
        actual=actual,
    )


def score_entry_return(target: dict[str, Any], observation: Observation | None, definition: Definition) -> ScoreResult:
    payload = observation.payload if observation else None
    current = target.get("current_price_usd")
    future = payload.get("price") if isinstance(payload, dict) else None
    signal = str(target.get("signal", "")).upper()
    if not isinstance(current, (int, float)) or current <= 0 or not isinstance(future, (int, float)):
        return ScoreResult(label="unavailable", status="unavailable", rationale="Price observation is incomplete.")
    move_pct = (float(future) - float(current)) / float(current) * 100
    if abs(move_pct) <= 1:
        return ScoreResult(label="neutral", score=0.0, status="neutral", actual={"move_pct": move_pct})
    actual = {"move_pct": move_pct, "direction": "up" if move_pct > 0 else "down"}
    correct = (signal == "ENTRY" and move_pct > 1) or (signal == "HOLD" and move_pct < -1)
    return _scored(actual["direction"], correct, actual)


def _fee_direction(value: float) -> str:
    if abs(value) <= 5:
        return "flat"
    return "up" if value > 0 else "down"


def score_fee_direction(target: dict[str, Any], observation: Observation | None, definition: Definition) -> ScoreResult:
    payload = observation.payload if observation else None
    value = payload.get("fees_7d_change_pct") if isinstance(payload, dict) else None
    predicted = target.get("prediction")
    predicted_direction = predicted.get("next_week_fee_direction") if isinstance(predicted, dict) else None
    if not isinstance(value, (int, float)) or predicted_direction not in {"up", "down", "flat"}:
        return ScoreResult(label="unavailable", status="unavailable", rationale="Fee observation or prediction is incomplete.")
    actual = {"fees_7d_change_pct": float(value), "direction": _fee_direction(float(value))}
    return _scored(actual["direction"], predicted_direction == actual["direction"], actual)


def _symbol_tokens(value: str) -> set[str]:
    return {token.upper() for token in re.findall(r"[A-Za-z0-9]+", value)}


def _observed_spread(payload: dict[str, Any]) -> float | None:
    pools = payload.get("yield", {}).get("pools", []) if isinstance(payload.get("yield"), dict) else []
    selected: dict[str, float] = {}
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        value = pool.get("apy_mean_30d")
        if value is None:
            value = pool.get("apy")
        if not isinstance(value, (int, float)):
            continue
        symbols = _symbol_tokens(str(pool.get("symbol", "")))
        for symbol in _DECENTRALIZED + _CENTRALIZED:
            if symbol.upper() in symbols and (symbol not in selected or float(value) > selected[symbol]):
                selected[symbol] = float(value)
    decentralized = [selected[symbol] for symbol in _DECENTRALIZED if symbol in selected]
    centralized = [selected[symbol] for symbol in _CENTRALIZED if symbol in selected]
    if not decentralized or not centralized:
        return None
    return sum(decentralized) / len(decentralized) - sum(centralized) / len(centralized)


def score_stablecoin_spread(target: dict[str, Any], observation: Observation | None, definition: Definition) -> ScoreResult:
    payload = observation.payload if observation else None
    current_metrics = target.get("basket_metrics")
    prediction = target.get("prediction")
    current = current_metrics.get("spread_pct") if isinstance(current_metrics, dict) else None
    predicted_direction = prediction.get("next_week_spread_direction") if isinstance(prediction, dict) else None
    predicted_winner = prediction.get("next_week_basket_winner") if isinstance(prediction, dict) else None
    future = _observed_spread(payload) if isinstance(payload, dict) else None
    if not isinstance(current, (int, float)) or future is None or predicted_direction not in {"widen", "narrow", "stable"}:
        return ScoreResult(
            label="unavailable",
            status="unavailable",
            rationale="Stablecoin observation or prediction is incomplete.",
        )
    change = abs(future) - abs(float(current))
    actual_direction = "widen" if change > 0.1 else "narrow" if change < -0.1 else "stable"
    actual_winner = "decentralized" if future >= 0.5 else "centralized" if future <= -0.5 else "tie"
    actual = {"spread_pct": future, "direction": actual_direction, "winner": actual_winner}
    correct = predicted_direction == actual_direction and (predicted_winner is None or predicted_winner == actual_winner)
    return _scored(actual_direction, correct, actual)


def register_builtin_adapters() -> None:
    register_parser("entry_timing", "1", parse_entry_timing)
    register_parser("eth_protocols", "1", parse_eth_protocols)
    register_parser("stablecoin_root", "1", parse_stablecoin_root)
    register_provider("token_price", "1", TokenPriceProvider())
    register_provider("protocol_fees", "1", ProtocolFeesProvider())
    register_provider("stablecoin_market", "1", StablecoinMarketProvider())
    register_scorer("entry_return", "1", score_entry_return)
    register_scorer("fee_direction", "1", score_fee_direction)
    register_scorer("stablecoin_spread", "1", score_stablecoin_spread)
