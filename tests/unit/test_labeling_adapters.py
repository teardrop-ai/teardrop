# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from labeling.adapters import (
    parse_entry_timing,
    parse_eth_protocols,
    parse_stablecoin_root,
    score_entry_return,
    score_fee_direction,
)
from labeling.contracts import Definition, Observation, ObservationRequest

_NOW = datetime.now(timezone.utc)


def _definition(parser: str = "entry") -> Definition:
    return Definition(key="example", version=1, parser_key=parser, config={"horizon_seconds": 60})


def test_entry_parser_expands_items_without_changing_payload():
    payload = {"task_class": "entry_timing_test", "tokens": [{"id": "one", "signal": "ENTRY"}]}
    targets = parse_entry_timing(payload, _definition(), _NOW)
    assert [target.item_key for target in targets] == ["one"]
    assert targets[0].item_payload == payload["tokens"][0]


def test_protocol_parser_rejects_duplicate_keys():
    payload = {"protocols": [{"slug": "a"}, {"slug": "a"}]}
    try:
        parse_eth_protocols(payload, _definition("eth"), _NOW)
    except ValueError as exc:
        assert "unique" in str(exc)
    else:
        raise AssertionError("duplicate protocol keys must be rejected")


def test_stablecoin_parser_creates_root_target():
    targets = parse_stablecoin_root({"task_class": "stablecoin_yield_compare"}, _definition("stable"), _NOW)
    assert [target.item_key for target in targets] == ["root"]


def test_entry_scorer_marks_neutral_band():
    request = ObservationRequest("token_price", "1", {"token": "one"}, _NOW)
    result = score_entry_return(
        {"current_price_usd": 100, "signal": "ENTRY"},
        Observation(request, {"price": 100.5}),
        _definition(),
    )
    assert result.status == "neutral"
    assert result.score == 0


@pytest.mark.parametrize(
    ("change_pct", "predicted_direction"),
    [
        (-5.01, "down"),
        (-5.0, "flat"),
        (-4.99, "flat"),
        (4.99, "flat"),
        (5.0, "flat"),
        (5.01, "up"),
    ],
)
def test_fee_scorer_uses_inclusive_five_percent_flat_threshold(change_pct, predicted_direction):
    request = ObservationRequest("protocol_fees", "1", {"protocol": "a"}, _NOW)
    result = score_fee_direction(
        {"prediction": {"next_week_fee_direction": predicted_direction}},
        Observation(request, {"fees_7d_change_pct": change_pct}),
        _definition("fees"),
    )
    assert result.status == "correct"
