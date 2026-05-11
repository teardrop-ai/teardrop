# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Scoring primitives for eval tasks."""

from __future__ import annotations

import json
import re
from typing import Any


def score_exact(expected: str, actual: str) -> float:
    return 1.0 if expected.strip() == actual.strip() else 0.0


def score_contains(expected_items: list[str], actual: str) -> float:
    if not expected_items:
        return 1.0
    lower = actual.lower()
    hits = sum(1 for item in expected_items if item.lower() in lower)
    return hits / len(expected_items)


def score_not_contains(excluded_items: list[str], actual: str) -> float:
    if not excluded_items:
        return 1.0
    lower = actual.lower()
    misses = sum(1 for item in excluded_items if item.lower() not in lower)
    return misses / len(excluded_items)


def score_json_shape(expected_shape: dict[str, Any], actual: str) -> float:
    try:
        payload = json.loads(actual)
    except Exception:
        return 0.0
    if not isinstance(payload, dict):
        return 0.0

    keys = list(expected_shape.keys())
    if not keys:
        return 1.0
    hits = sum(1 for key in keys if key in payload)
    return hits / len(keys)


def score_contains_pattern(expected_patterns: list[str], actual: str) -> float:
    if not expected_patterns:
        return 1.0
    hits = sum(1 for pattern in expected_patterns if re.search(pattern, actual))
    return hits / len(expected_patterns)


def score_task(
    *,
    scorer: str,
    expected_text_contains: list[str],
    actual_text: str,
    expected_text_not_contains: list[str] | None = None,
) -> float:
    negative_score = score_not_contains(expected_text_not_contains or [], actual_text)

    if scorer == "contains":
        return score_contains(expected_text_contains, actual_text) * negative_score
    if scorer == "contains_pattern":
        return score_contains_pattern(expected_text_contains, actual_text) * negative_score
    if scorer == "exact":
        if not expected_text_contains:
            return negative_score
        return score_exact(expected_text_contains[0], actual_text) * negative_score
    if scorer == "not_contains":
        return negative_score
    # Default and fallback: contains. LLM-judge can be plugged in later.
    return score_contains(expected_text_contains, actual_text) * negative_score
