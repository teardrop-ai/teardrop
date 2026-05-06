# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Scoring primitives for eval tasks."""

from __future__ import annotations

import json
from typing import Any


def score_exact(expected: str, actual: str) -> float:
    return 1.0 if expected.strip() == actual.strip() else 0.0


def score_contains(expected_items: list[str], actual: str) -> float:
    if not expected_items:
        return 1.0
    lower = actual.lower()
    hits = sum(1 for item in expected_items if item.lower() in lower)
    return hits / len(expected_items)


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


def score_task(*, scorer: str, expected_text_contains: list[str], actual_text: str) -> float:
    if scorer == "contains":
        return score_contains(expected_text_contains, actual_text)
    if scorer == "exact":
        if not expected_text_contains:
            return 1.0
        return score_exact(expected_text_contains[0], actual_text)
    # Default and fallback: contains. LLM-judge can be plugged in later.
    return score_contains(expected_text_contains, actual_text)
