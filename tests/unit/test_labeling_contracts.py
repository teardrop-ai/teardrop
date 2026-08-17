# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from labeling.contracts import ObservationRequest, ScoreResult, TargetDraft, canonical_json, validate_prediction


def test_canonical_json_is_stable_and_bounded():
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert validate_prediction({"task_class": "example"}, {"type": "object"})


def test_canonical_json_rejects_deep_payloads():
    value: object = {}
    for _ in range(17):
        value = {"nested": value}
    with pytest.raises(ValueError, match="nesting"):
        canonical_json(value)


def test_target_draft_requires_ordered_timezone_windows():
    now = datetime.now(timezone.utc)
    target = TargetDraft("one", {"value": 1}, now, now + timedelta(days=1), now + timedelta(days=1))
    assert target.window_start.tzinfo is not None
    with pytest.raises(ValueError, match="window_start"):
        TargetDraft("one", {}, now + timedelta(days=1), now, now)


def test_observation_scope_is_explicit():
    request = ObservationRequest("provider", "1", {"id": "one"}, datetime.now(timezone.utc), "org:one")
    assert request.scope_key == "org:one"


def test_score_result_rejects_pending_score_shape():
    with pytest.raises(ValueError, match="Non-scored"):
        ScoreResult(label="waiting", status="unavailable", score=0)
