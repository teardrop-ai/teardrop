# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from labeling import scorecards
from labeling.contracts import Definition

_DAY0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
_NOW = _DAY0 + timedelta(days=10)
_CONFIG = {"round_seconds": 86400, "horizon_seconds": 86400, "finality_seconds": 1800, "min_sample": 2}
_P = {"lt25": 0.6, "b25_50": 0.1, "b50_100": 0.1, "b100_200": 0.1, "gt200": 0.1}


def _row(subject: str, day: int, *, status: str | None = "correct", score: float | None = 0.2, label: str = "lt25"):
    start = _DAY0 + timedelta(days=day)
    return {
        "subject": subject,
        "window_start": start,
        "window_end": start + timedelta(days=1),
        "item_payload": {"p": _P},
        "status": status,
        "score": Decimal(str(score)) if score is not None else None,
        "label": label,
    }


def _cards(rows, **kwargs):
    return {card["subject"]: card for card in scorecards.summarize(rows, _CONFIG, _NOW, **kwargs)}


def test_summarize_imputes_missed_rounds_at_uniform_baseline():
    subject = "0x" + "aa" * 20
    rows = [
        _row(subject, 1, score=0.2),
        _row(subject, 2, status="incorrect", score=0.3, label="gt200"),
        _row(subject, 4, score=0.1),
    ]

    card = _cards(rows)[subject]

    assert card["eligible"] is True
    assert card["rounds_expected"] == 8
    assert card["rounds_submitted"] == 3
    assert card["coverage"] == pytest.approx(3 / 8)
    assert card["mean_brier"] == pytest.approx(0.2)
    assert card["adjusted_brier"] == pytest.approx((0.6 + 5 * 0.8) / 8)
    assert card["accuracy"] == pytest.approx(2 / 3)
    assert card["platform_attested"] is False
    assert "calibration" not in card


def test_summarize_excludes_unavailable_and_pending_from_scores():
    subject = "0x" + "bb" * 20
    rows = [
        _row(subject, 1),
        _row(subject, 2),
        _row(subject, 3, status="unavailable", score=None, label="unavailable"),
        _row(subject, 4, status=None, score=None),
        _row(subject, 9, status=None, score=None),
    ]

    card = _cards(rows)[subject]

    assert card["n_scored"] == 2
    assert card["unresolved"] == 2
    assert card["rounds_submitted"] == 5
    assert card["mean_brier"] == pytest.approx(0.2)
    assert card["adjusted_brier"] == pytest.approx((0.4 + 4 * 0.8) / 6)


def test_summarize_withholds_metrics_below_min_sample_and_flags_platform_agents():
    card = _cards([_row("schedule:abc", 1)])["schedule:abc"]

    assert card["eligible"] is False
    assert card["platform_attested"] is True
    assert card["n_scored"] == 1
    assert card["mean_brier"] is None
    assert card["adjusted_brier"] is None
    assert card["coverage"] is None


def test_unavailable_round_is_excluded_for_every_subject():
    submitted, skipped = "0x" + "11" * 20, "0x" + "22" * 20
    rows = [_row(subject, day) for subject in (submitted, skipped) for day in (1, 2)]
    rows.append(_row(submitted, 3, status="unavailable", score=None, label="unavailable"))

    cards = _cards(rows)

    assert cards[submitted]["adjusted_brier"] == cards[skipped]["adjusted_brier"]
    assert cards[submitted]["rounds_expected"] == cards[skipped]["rounds_expected"] == 7


def test_coverage_starts_at_window_boundary_not_first_recent_submission():
    subject = "schedule:returning"
    rows = [{**_row(subject, day), "first_round": _DAY0 - timedelta(days=60)} for day in (7, 8)]

    card = _cards(rows, window_start=_DAY0 + timedelta(hours=12))[subject]

    assert card["rounds_expected"] == 8
    assert card["adjusted_brier"] == pytest.approx((0.4 + 6 * 0.8) / 8)


def test_summarize_orders_eligible_by_adjusted_brier_and_adds_calibration():
    better, worse = "0x" + "01" * 20, "0x" + "02" * 20
    rows = [_row(worse, day, score=0.5) for day in range(1, 9)] + [_row(better, day, score=0.1) for day in range(1, 9)]
    rows.append(_row("schedule:new", 8))

    ordered = scorecards.summarize(rows, _CONFIG, _NOW, calibration=True)

    assert [card["subject"] for card in ordered] == [better, worse, "schedule:new"]
    bins = {item["bin"]: item for item in ordered[0]["calibration"]}
    assert bins[6]["count"] == 8
    assert bins[6]["observed_frequency"] == pytest.approx(1.0)
    assert bins[1]["count"] == 32
    assert bins[1]["observed_frequency"] == pytest.approx(0.0)


def test_definition_sha256_binds_config():
    definition = Definition(key="oracle_deviation", version=1, config={"min_sample": 30})
    assert scorecards.definition_sha256(definition) == scorecards.definition_sha256(dataclasses.replace(definition))
    changed = dataclasses.replace(definition, config={"min_sample": 1})
    assert scorecards.definition_sha256(definition) != scorecards.definition_sha256(changed)


@pytest.mark.anyio
async def test_compute_scorecards_is_cached_and_parameterized(monkeypatch):
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[_row("0x" + "cc" * 20, 1)])
    monkeypatch.setattr(scorecards, "_get_pool", lambda: pool)
    monkeypatch.setattr(scorecards, "_cache", {})
    definition = Definition(key="oracle_deviation", version=1, config=_CONFIG)

    first = await scorecards.compute_scorecards(definition, 90)
    second = await scorecards.compute_scorecards(definition, 90)
    subject = await scorecards.compute_scorecards(definition, 90, "0x" + "cc" * 20)

    assert first == second
    assert subject[0]["subject"] == first[0]["subject"]
    pool.fetch.assert_awaited_once()
    args = pool.fetch.await_args.args
    assert args[1:3] == ("oracle_deviation", 1)
    assert len(args) == 4
    assert abs((datetime.now(timezone.utc) - args[3]) - timedelta(days=91, seconds=1800)) < timedelta(minutes=1)


@pytest.mark.anyio
async def test_thirty_day_window_can_reach_thirty_scored_rounds(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW

    rows = [_row("schedule:daily", day) for day in range(-21, 9)]

    async def fetch(query, key, version, cutoff):
        return [row for row in rows if row["window_start"] >= cutoff]

    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(scorecards, "_get_pool", lambda: pool)
    monkeypatch.setattr(scorecards, "_cache", {})
    monkeypatch.setattr(scorecards, "datetime", FrozenDatetime)
    definition = Definition(key="oracle_deviation", version=1, config={**_CONFIG, "min_sample": 30})

    (card,) = await scorecards.compute_scorecards(definition, 30)

    assert card["n_scored"] == card["rounds_expected"] == 30
    assert card["eligible"] is True
