# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tools.definitions.record_predictions import RecordPredictionsInput, record_predictions


def test_record_predictions_input_accepts_nested_json():
    value = RecordPredictionsInput.model_validate(
        {"predictions": {"task_class": "example", "items": [{"id": "one", "score": None}]}}
    )
    assert value.predictions["items"][0]["id"] == "one"


@pytest.mark.asyncio
async def test_record_predictions_is_side_effect_free():
    assert await record_predictions(predictions={"task_class": "example"}) == {"recorded": True}


def test_record_predictions_input_rejects_oversized_payload():
    with pytest.raises(ValidationError, match="allowed size"):
        RecordPredictionsInput.model_validate({"predictions": {"report": "x" * (64 * 1024)}})


def test_record_predictions_input_rejects_non_json_number():
    with pytest.raises(ValidationError, match="JSON-compatible"):
        RecordPredictionsInput.model_validate({"predictions": {"score": float("nan")}})
