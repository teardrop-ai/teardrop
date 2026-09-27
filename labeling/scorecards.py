# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Public scorecards derived on read from committed, anchored, automatically scored predictions.

Nothing here is stored. Eligibility rules:

* the definition is active and pre-registered with ``config.public = true``;
* the prediction is accepted, committed, and its batch was confirmed on-chain before the
  target window closed;
* only the first prediction per (subject, round, item) counts;
* only the earliest automatic result with a pinned observation counts; overrides never do.

Missed rounds are imputed at the uniform-forecast Brier score so selective submission cannot
improve a subject's ranking.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from labeling.contracts import Definition, canonical_json
from labeling.store import _definition_from_row, _get_pool

_SCORED = frozenset({"correct", "incorrect", "neutral"})
_CACHE_TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 256
_CALIBRATION_BINS = 10
_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}

_DEFINITION_COLUMNS = """
    definition_key, definition_version, prediction_schema, target_schema,
    outcome_schema, parser_key, parser_version, provider_key, provider_version,
    scorer_key, scorer_version, config
"""

_ROWS_SQL = """
WITH first_predictions AS (
    SELECT DISTINCT ON (s.subject, t.window_start, t.item_key)
           s.subject, t.id AS target_id, t.window_start, t.window_end, t.item_payload,
           b.block_number, b.anchored_at
    FROM labeling_predictions p
    CROSS JOIN LATERAL (
        SELECT COALESCE(p.signer_address, 'schedule:' || p.schedule_id) AS subject
    ) s
    LEFT JOIN commitment_batches b ON b.id = p.anchor_batch_id
    JOIN labeling_targets t ON t.prediction_id = p.id AND t.org_id = p.org_id
    WHERE p.definition_key = $1
      AND p.definition_version = $2
      AND p.status = 'accepted'
      AND p.leaf_sha256 IS NOT NULL
    ORDER BY s.subject, t.window_start, t.item_key, p.prediction_at, p.id
), eligible AS (
    SELECT * FROM first_predictions
    WHERE block_number IS NOT NULL AND anchored_at < window_end
), subjects AS (
    SELECT subject, MIN(window_start) AS first_round FROM eligible GROUP BY subject
)
SELECT s.subject, s.first_round, e.window_start, e.window_end, e.item_payload, r.status, r.score, r.label
FROM subjects s
LEFT JOIN eligible e ON e.subject = s.subject AND e.window_start >= $3
LEFT JOIN LATERAL (
    SELECT status, score, label
    FROM labeling_results
    WHERE target_id = e.target_id AND source = 'automatic' AND observation_id IS NOT NULL
    ORDER BY created_at, id
    LIMIT 1
) r ON TRUE
ORDER BY s.subject, e.window_start
"""


def definition_sha256(definition: Definition) -> str:
    return hashlib.sha256(canonical_json(dataclasses.asdict(definition)).encode("utf-8")).hexdigest()


def _cached(key: tuple[Any, ...]) -> Any | None:
    entry = _cache.get(key)
    if entry is None or entry[0] <= time.monotonic():
        return None
    return entry[1]


def _store(key: tuple[Any, ...], value: Any) -> Any:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, value)
    return value


async def list_public_definitions() -> list[Definition]:
    rows = await _get_pool().fetch(
        f"""
        SELECT {_DEFINITION_COLUMNS}
        FROM labeling_definitions
        WHERE active = TRUE AND config->>'public' = 'true'
        ORDER BY definition_key, definition_version
        """
    )
    return [_definition_from_row(row) for row in rows]


async def get_public_definition(key: str, version: int) -> Definition | None:
    row = await _get_pool().fetchrow(
        f"""
        SELECT {_DEFINITION_COLUMNS}
        FROM labeling_definitions
        WHERE definition_key = $1 AND definition_version = $2
          AND active = TRUE AND config->>'public' = 'true'
        """,
        key,
        version,
    )
    return _definition_from_row(row) if row is not None else None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _calibration(scored: Sequence[tuple[Mapping[str, Any], str]]) -> list[dict[str, Any]]:
    counts = [0] * _CALIBRATION_BINS
    probability_sums = [0.0] * _CALIBRATION_BINS
    hits = [0] * _CALIBRATION_BINS
    for forecast, outcome in scored:
        for label, p in forecast.items():
            index = min(int(float(p) * _CALIBRATION_BINS), _CALIBRATION_BINS - 1)
            counts[index] += 1
            probability_sums[index] += float(p)
            hits[index] += 1 if label == outcome else 0
    return [
        {
            "bin": index,
            "lower": index / _CALIBRATION_BINS,
            "upper": (index + 1) / _CALIBRATION_BINS,
            "count": counts[index],
            "mean_probability": _round(probability_sums[index] / counts[index]),
            "observed_frequency": _round(hits[index] / counts[index]),
        }
        for index in range(_CALIBRATION_BINS)
        if counts[index]
    ]


def summarize(
    rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    now: datetime,
    *,
    calibration: bool = False,
    window_start: datetime | None = None,
) -> list[dict[str, Any]]:
    """Aggregate eligible target rows into one scorecard per subject."""
    round_seconds = int(config["round_seconds"])
    finality = timedelta(seconds=int(config["finality_seconds"]))
    settle_seconds = int(config["horizon_seconds"]) + int(config["finality_seconds"])
    min_sample = max(1, int(config.get("min_sample", 1)))
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    unavailable = {row["window_start"] for row in rows if row["status"] == "unavailable" and row["window_end"] + finality <= now}
    for row in rows:
        grouped[str(row["subject"])].append(row)

    cards: list[dict[str, Any]] = []
    for subject, items in grouped.items():
        briers: list[float] = []
        scored: list[tuple[Mapping[str, Any], str]] = []
        correct = incorrect = unresolved = 0
        submitted: set[datetime] = set()
        settled: set[datetime] = set()
        classes = len(config.get("buckets_bps", [])) + 1
        for item in items:
            if item["window_start"] is None:
                continue
            forecast = (item["item_payload"] or {}).get("p") or {}
            classes = max(classes, len(forecast))
            submitted.add(item["window_start"])
            if item["window_end"] + finality > now:
                continue
            if item["window_start"] in unavailable:
                unresolved += 1
                continue
            settled.add(item["window_start"])
            if item["status"] in _SCORED and item["score"] is not None:
                briers.append(float(item["score"]))
                scored.append((forecast, str(item["label"])))
                correct += item["status"] == "correct"
                incorrect += item["status"] == "incorrect"
            else:
                unresolved += 1

        first = min(item.get("first_round") or item["window_start"] for item in items)
        if window_start is not None:
            first = max(
                first,
                datetime.fromtimestamp(math.ceil(window_start.timestamp() / round_seconds) * round_seconds, tz=timezone.utc),
            )
        elapsed = (now - first).total_seconds() - settle_seconds
        expected = max(0, math.floor(elapsed / round_seconds) + 1) if elapsed >= 0 else 0
        expected -= sum(start >= first for start in unavailable)
        missed = max(0, expected - len(settled))
        baseline = 1.0 - 1.0 / classes if classes else 0.0
        n = len(briers)
        eligible = n >= min_sample
        card: dict[str, Any] = {
            "subject": subject,
            "platform_attested": subject.startswith("schedule:"),
            "eligible": eligible,
            "n_scored": n,
            "rounds_submitted": len(submitted),
            "rounds_expected": expected,
            "coverage": None,
            "unresolved": unresolved,
            "mean_brier": None,
            "adjusted_brier": None,
            "accuracy": None,
        }
        if eligible:
            card["coverage"] = _round(len(settled) / expected) if expected else None
            card["mean_brier"] = _round(math.fsum(briers) / n)
            card["adjusted_brier"] = _round((math.fsum(briers) + missed * baseline) / (n + missed))
            card["accuracy"] = _round(correct / (correct + incorrect)) if correct + incorrect else None
            if calibration:
                card["calibration"] = _calibration(scored)
        cards.append(card)

    cards.sort(key=lambda card: (not card["eligible"], card["adjusted_brier"] or 0.0, card["subject"]))
    return cards


async def compute_scorecards(definition: Definition, window_days: int, subject: str | None = None) -> list[dict[str, Any]]:
    key = (definition.key, definition.version, window_days)
    cached = _cached(key)
    if cached is None:
        now = datetime.now(timezone.utc)
        settle_seconds = int(definition.config["horizon_seconds"]) + int(definition.config["finality_seconds"])
        window_start = now - timedelta(days=window_days, seconds=settle_seconds)
        rows = await _get_pool().fetch(
            _ROWS_SQL,
            definition.key,
            definition.version,
            window_start,
        )
        cached = _store(key, summarize(list(rows), definition.config, now, calibration=True, window_start=window_start))
    if subject is not None:
        return [card for card in cached if card["subject"] == subject]
    return [{field: value for field, value in card.items() if field != "calibration"} for card in cached]
