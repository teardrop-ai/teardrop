# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Structured prediction capture tool for scheduled labeling runs."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, model_validator

from tools.registry import ToolDefinition


class RecordPredictionsInput(BaseModel):
    predictions: dict[str, Any] = Field(
        ...,
        description="The complete structured prediction document for this run.",
    )

    @model_validator(mode="after")
    def validate_payload(self) -> "RecordPredictionsInput":
        try:
            encoded = json.dumps(self.predictions, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Prediction payload must contain JSON-compatible values") from exc
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError("Prediction payload exceeds the allowed size")
        return self


class RecordPredictionsOutput(BaseModel):
    recorded: bool


async def record_predictions(*, predictions: dict[str, Any]) -> dict[str, bool]:
    return {"recorded": True}


TOOL = ToolDefinition(
    name="record_predictions",
    version="1.0.0",
    description=(
        "Record a complete structured prediction document for downstream evaluation. "
        "Call once with the exact machine-readable prediction; provide the human-readable report separately."
    ),
    tags=["internal", "predictions", "labeling"],
    input_schema=RecordPredictionsInput,
    output_schema=RecordPredictionsOutput,
    capture_args=True,
    show_on_agent_card=False,
    implementation=record_predictions,
)
