# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from eth_abi import encode as abi_encode
from web3 import Web3
from web3.exceptions import ContractLogicError

from labeling import onchain
from labeling.contracts import Definition, Observation, ObservationRequest

_BOUNDS = [25, 50, 100, 200]
_LABELS = ["lt25", "b25_50", "b50_100", "b100_200", "gt200"]
_FEED = "0x" + "11" * 20
_POOL = "0x" + "22" * 20
_CONFIG = {
    "public": True,
    "chain_id": 8453,
    "feed": _FEED,
    "feed_decimals": 8,
    "pool": _POOL,
    "token0_decimals": 18,
    "token1_decimals": 6,
    "twap_seconds": 1800,
    "round_seconds": 86400,
    "horizon_seconds": 86400,
    "finality_seconds": 1800,
    "buckets_bps": _BOUNDS,
    "max_stale_seconds": 3600,
    "min_sample": 30,
}
_DEFINITION = Definition(
    key="oracle_deviation",
    version=1,
    parser_key="oracle_rounds",
    provider_key="oracle_deviation",
    scorer_key="oracle_deviation_brier",
    config=_CONFIG,
)
_FORECAST = {"lt25": 0.1, "b25_50": 0.6, "b50_100": 0.2, "b100_200": 0.05, "gt200": 0.05}
_TICK = -198079


def _twap(tick: int) -> Decimal:
    return (Decimal("1.0001") ** tick).scaleb(12)


def _snapshot(*, block: int, timestamp: int, deviation: Decimal, age: int = 60, tick: int = _TICK, delta_extra: int = 0):
    answer = int(_twap(tick) * (1 + deviation) * Decimal(10**8))
    return {
        "block": block,
        "hash": "0x" + "ab" * 32,
        "timestamp": timestamp,
        "latest_round_data": Web3.to_hex(
            abi_encode(["uint80", "int256", "uint256", "uint256", "uint80"], [7, answer, timestamp - age, timestamp - age, 7])
        ),
        "slot0": Web3.to_hex(
            abi_encode(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], [1, tick, 0, 10, 10, 0, True])
        ),
        "observe": Web3.to_hex(abi_encode(["int56[]", "uint160[]"], [[0, tick * 1800 + delta_extra], [0, 0]])),
    }


def _observation(start: dict, end: dict) -> Observation:
    request = ObservationRequest("oracle_deviation", "1", {"start": "x"}, datetime(2026, 9, 27, tzinfo=timezone.utc))
    return Observation(request=request, payload={"start": start, "end": end})


def test_bucket_labels_cover_full_range():
    assert onchain.bucket_labels(_BOUNDS) == _LABELS


@pytest.mark.parametrize("bounds", [[], [50, 25], [25, 25], [0, 25], [True, 25], "25", None])
def test_bucket_labels_reject_degenerate_bounds(bounds):
    with pytest.raises(ValueError):
        onchain.bucket_labels(bounds)


@pytest.mark.parametrize(
    ("value", "label"),
    [("0", "lt25"), ("24.9999", "lt25"), ("25", "b25_50"), ("199.9999", "b100_200"), ("200", "gt200"), ("5000", "gt200")],
)
def test_bucket_for_uses_half_open_intervals(value, label):
    assert onchain.bucket_for(Decimal(value), _BOUNDS) == label


@pytest.mark.parametrize(
    "value",
    [
        {**_FORECAST, "extra": 0.0},
        {k: v for k, v in _FORECAST.items() if k != "gt200"},
        {**_FORECAST, "lt25": -0.1, "b25_50": 0.8},
        {**_FORECAST, "lt25": float("nan")},
        {**_FORECAST, "lt25": True},
        {**_FORECAST, "lt25": 0.2},
        None,
    ],
)
def test_probabilities_reject_invalid_forecasts(value):
    with pytest.raises(ValueError):
        onchain.probabilities(value, _LABELS)


def test_round_window_starts_strictly_after_prediction():
    boundary = datetime(2026, 9, 27, tzinfo=timezone.utc)
    start, end, due = onchain.round_window(boundary, _CONFIG)
    assert start == boundary + timedelta(days=1)
    assert end == start + timedelta(days=1)
    assert due == end + timedelta(seconds=1800)
    assert onchain.round_window(boundary - timedelta(seconds=1), _CONFIG)[0] == boundary


def test_parse_oracle_rounds_creates_single_root_target():
    prediction_at = datetime(2026, 9, 26, 15, 30, tzinfo=timezone.utc)
    (target,) = onchain.parse_oracle_rounds({"deviation_bps": _FORECAST}, _DEFINITION, prediction_at)
    assert target.item_key == "root"
    assert target.item_payload == {"p": _FORECAST}
    assert target.window_start == datetime(2026, 9, 27, tzinfo=timezone.utc)
    assert target.due_at == target.window_end + timedelta(seconds=1800)
    with pytest.raises(ValueError):
        onchain.parse_oracle_rounds({"deviation_bps": {"lt25": 1.0}}, _DEFINITION, prediction_at)


def test_brier_result_statuses():
    perfect = onchain.brier_result({"a": 1.0, "b": 0.0}, "a", {})
    assert (perfect.status, perfect.score) == ("correct", 0.0)
    wrong = onchain.brier_result({"a": 0.8, "b": 0.2}, "b", {})
    assert wrong.status == "incorrect"
    assert wrong.score == pytest.approx(1.28)
    assert onchain.brier_result({"a": 0.5, "b": 0.5}, "a", {}).status == "neutral"


def test_score_oracle_deviation_buckets_end_deviation_and_is_deterministic():
    start = _snapshot(block=10, timestamp=1_000, deviation=Decimal("0.0001"))
    end = _snapshot(block=20, timestamp=87_400, deviation=Decimal("0.003"))
    observation = _observation(start, end)

    result = onchain.score_oracle_deviation({"p": _FORECAST}, observation, _DEFINITION)

    assert result.status == "correct"
    assert result.label == "b25_50"
    assert result.score == pytest.approx(0.215)
    assert result.actual["end"]["block"] == 20
    assert result.actual["end"]["round_id"] == "7"
    assert Decimal(result.actual["start_deviation_bps"]) < 25
    assert onchain.score_oracle_deviation({"p": _FORECAST}, observation, _DEFINITION) == result


@pytest.mark.parametrize(
    "mutate",
    [
        lambda end: end.update(latest_round_data="0x"),
        lambda end: end.update(slot0="0x" + "00" * 64),
        lambda end: end.update(observe="0x"),
        lambda end: end.update(
            latest_round_data=_snapshot(block=20, timestamp=87_400, deviation=Decimal(0), age=3601)["latest_round_data"]
        ),
        lambda end: end.pop("timestamp"),
        lambda end: end.update(
            latest_round_data=Web3.to_hex(
                abi_encode(["uint80", "int256", "uint256", "uint256", "uint80"], [7, 2**255 - 1, 87_340, 87_340, 7])
            )
        ),
        lambda end: end.update(
            latest_round_data=Web3.to_hex(
                abi_encode(["uint80", "int256", "uint256", "uint256", "uint80"], [7, 2000 * 10**8, 87_340, 87_340, 6])
            )
        ),
        lambda end: end.update(observe=Web3.to_hex(abi_encode(["int56[]", "uint160[]"], [[0, 887273 * 1800], [0, 0]]))),
    ],
)
def test_score_oracle_deviation_fails_closed(mutate):
    start = _snapshot(block=10, timestamp=1_000, deviation=Decimal(0))
    end = _snapshot(block=20, timestamp=87_400, deviation=Decimal(0))
    mutate(end)

    result = onchain.score_oracle_deviation({"p": _FORECAST}, _observation(start, end), _DEFINITION)

    assert result.status == "unavailable"
    assert result.score is None


def test_score_oracle_deviation_stale_start_is_unavailable():
    start = _snapshot(block=10, timestamp=100_000, deviation=Decimal(0), age=3601)
    end = _snapshot(block=20, timestamp=186_400, deviation=Decimal(0))
    assert onchain.score_oracle_deviation({"p": _FORECAST}, _observation(start, end), _DEFINITION).status == "unavailable"


def test_mean_tick_rounds_toward_negative_infinity():
    snapshot = _snapshot(block=1, timestamp=1_000, deviation=Decimal(0), tick=-2, delta_extra=-1)
    _, evidence = onchain._deviation(snapshot, _CONFIG)
    assert evidence["mean_tick"] == -3


class _FakeEth:
    def __init__(self, *, head_number: int, genesis: int = 1_000, revert_slot0: bool = False, fail_calls: bool = False):
        self.head_number = head_number
        self.genesis = genesis
        self.revert_slot0 = revert_slot0
        self.fail_calls = fail_calls
        self.calls: list[tuple[str, int]] = []

    def _block(self, number: int) -> dict:
        return {"number": number, "timestamp": self.genesis + 2 * number, "hash": bytes([number % 256]) * 32}

    async def get_block(self, identifier):
        return self._block(self.head_number if identifier == "finalized" else int(identifier))

    async def call(self, transaction, block_identifier):
        if self.fail_calls:
            raise OSError("connection reset")
        data = bytes(transaction["data"])
        self.calls.append((transaction["to"].lower(), block_identifier))
        if data == onchain._SLOT0 and self.revert_slot0:
            raise ContractLogicError("execution reverted")
        return b"\x01" * 32


@pytest.fixture
def direct_rpc(monkeypatch):
    async def _direct(coro_fn, timeout_seconds=None, chain_id=None):
        return await coro_fn()

    monkeypatch.setattr(onchain, "rpc_call", _direct)


@pytest.mark.anyio
@pytest.mark.parametrize(("timestamp", "expected"), [(1_000, 0), (1_001, 1), (1_002, 1), (1_199, 100), (1_200, 100)])
async def test_first_block_at_or_after(direct_rpc, timestamp, expected):
    eth = _FakeEth(head_number=100)
    w3 = SimpleNamespace(eth=eth)
    head = await eth.get_block("finalized")
    block = await onchain.first_block_at_or_after(w3, 8453, timestamp, head)
    assert block["number"] == expected


@pytest.mark.anyio
async def test_first_block_waits_for_finality(direct_rpc):
    eth = _FakeEth(head_number=100)
    assert await onchain.first_block_at_or_after(SimpleNamespace(eth=eth), 8453, 1_201, await eth.get_block("finalized")) is None


def _request(start: datetime, end: datetime) -> ObservationRequest:
    from labeling.contracts import TargetDraft

    target = TargetDraft("root", {"p": _FORECAST}, start, end, end)
    return onchain.OracleDeviationProvider().plan(target, _DEFINITION)


@pytest.mark.anyio
async def test_provider_pins_both_blocks_and_records_reverts(direct_rpc, monkeypatch):
    eth = _FakeEth(head_number=100_000, revert_slot0=True)
    monkeypatch.setattr(onchain, "get_web3", lambda chain_id: SimpleNamespace(eth=eth))
    start = datetime.fromtimestamp(1_000 + 2 * 500, tz=timezone.utc)
    end = datetime.fromtimestamp(1_000 + 2 * 900, tz=timezone.utc)
    request = _request(start, end)

    observations = await onchain.OracleDeviationProvider().fetch_batch([request], _DEFINITION)

    payload = observations[request.request_sha256].payload
    assert (payload["start"]["block"], payload["end"]["block"]) == (500, 900)
    assert payload["start"]["slot0"] == "0x"
    assert payload["end"]["latest_round_data"] == "0x" + "01" * 32
    assert payload["end"]["hash"].startswith("0x")
    assert {call[0] for call in eth.calls} == {_FEED, _POOL}
    assert {call[1] for call in eth.calls} == {500, 900}


@pytest.mark.anyio
async def test_provider_omits_unfinalized_and_failed_requests(direct_rpc, monkeypatch):
    eth = _FakeEth(head_number=100)
    monkeypatch.setattr(onchain, "get_web3", lambda chain_id: SimpleNamespace(eth=eth))
    start = datetime.fromtimestamp(1_010, tz=timezone.utc)
    late = _request(start, datetime.fromtimestamp(1_000 + 2 * 101, tz=timezone.utc))
    assert await onchain.OracleDeviationProvider().fetch_batch([late], _DEFINITION) == {}

    eth.fail_calls = True
    ready = _request(start, datetime.fromtimestamp(1_100, tz=timezone.utc))
    assert await onchain.OracleDeviationProvider().fetch_batch([ready], _DEFINITION) == {}


@pytest.mark.anyio
async def test_provider_head_failure_logs_type_only(direct_rpc, monkeypatch, caplog):
    secret_url = "https://base.example/v2/SECRET-KEY"

    def _unconfigured(chain_id):
        raise ValueError(f"cannot reach {secret_url}")

    monkeypatch.setattr(onchain, "get_web3", _unconfigured)
    request = _request(datetime.fromtimestamp(1_010, tz=timezone.utc), datetime.fromtimestamp(1_100, tz=timezone.utc))

    assert await onchain.OracleDeviationProvider().fetch_batch([request], _DEFINITION) == {}
    assert "SECRET-KEY" not in caplog.text
    assert "ValueError" in caplog.text


def test_plan_request_identity_is_per_round():
    day = datetime(2026, 9, 27, tzinfo=timezone.utc)
    first = _request(day, day + timedelta(days=1))
    second = _request(day + timedelta(days=1), day + timedelta(days=2))
    assert first.request_sha256 == _request(day, day + timedelta(days=1)).request_sha256
    assert first.request_sha256 != second.request_sha256
