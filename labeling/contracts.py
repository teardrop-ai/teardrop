# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from jsonschema import Draft7Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_PREDICTION_BYTES = 64 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_CONTAINER_ITEMS = 10_000


def _validate_json_shape(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("Prediction payload nesting exceeds the allowed limit")
    if isinstance(value, dict):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise ValueError("Prediction payload contains too many object fields")
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("Prediction payload keys must be strings")
            _validate_json_shape(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise ValueError("Prediction payload contains too many array items")
        for child in value:
            _validate_json_shape(child, depth + 1)


def canonical_json(value: Any) -> str:
    _validate_json_shape(value)
    try:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Prediction payload must contain JSON-compatible values") from exc
    if len(encoded.encode("utf-8")) > MAX_PREDICTION_BYTES:
        raise ValueError("Prediction payload exceeds the allowed size")
    return encoded


def validate_prediction(payload: dict[str, Any], schema: Mapping[str, Any] | None = None) -> str:
    if not isinstance(payload, dict):
        raise ValueError("Prediction payload must be a JSON object")
    encoded = canonical_json(payload)
    if schema:
        try:
            Draft7Validator.check_schema(dict(schema))
            Draft7Validator(dict(schema)).validate(payload)
        except Exception as exc:
            raise ValueError("Prediction payload failed its registered schema") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Prediction timestamps must include a timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Definition:
    key: str
    version: int
    prediction_schema: dict[str, Any] = field(default_factory=dict)
    target_schema: dict[str, Any] = field(default_factory=dict)
    outcome_schema: dict[str, Any] = field(default_factory=dict)
    parser_key: str = "root"
    parser_version: str = "1"
    provider_key: str = ""
    provider_version: str = "1"
    scorer_key: str = ""
    scorer_version: str = "1"
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TargetDraft:
    item_key: str
    item_payload: dict[str, Any]
    window_start: datetime
    window_end: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        if not self.item_key or len(self.item_key) > 256:
            raise ValueError("Target item_key must contain 1-256 characters")
        if not isinstance(self.item_payload, dict):
            raise ValueError("Target payload must be a JSON object")
        start = utc_datetime(self.window_start)
        end = utc_datetime(self.window_end)
        due = utc_datetime(self.due_at)
        if start >= end:
            raise ValueError("Target window_start must precede window_end")
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        object.__setattr__(self, "due_at", due)


@dataclass(frozen=True, slots=True)
class ObservationRequest:
    provider_key: str
    provider_version: str
    request: dict[str, Any]
    as_of: datetime
    scope_key: str = "public"

    def __post_init__(self) -> None:
        utc_datetime(self.as_of)
        if not self.scope_key or len(self.scope_key) > 256:
            raise ValueError("Observation scope_key must contain 1-256 characters")
        object.__setattr__(self, "as_of", utc_datetime(self.as_of))
        canonical_json(self.request)

    @property
    def request_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.request).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Observation:
    request: ObservationRequest
    payload: dict[str, Any] | None
    status: Literal["ready", "unavailable", "invalid"] = "ready"
    error: str = ""


class ScoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=128)
    score: float | None = None
    status: Literal["correct", "incorrect", "neutral", "inconclusive", "unavailable", "invalid"]
    actual: dict[str, Any] | None = None
    rationale: str = Field(default="", max_length=2000)
    source: Literal["automatic", "external", "manual"] = "automatic"

    @model_validator(mode="after")
    def validate_score_state(self) -> "ScoreResult":
        scored = {"correct", "incorrect", "neutral"}
        if self.status in scored and self.score is None:
            raise ValueError("Scored results require a numeric score")
        if self.status not in scored and self.score is not None:
            raise ValueError("Non-scored results cannot contain a score")
        return self


class PredictionToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predictions: dict[str, Any]


class PredictionToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recorded: bool

    @model_validator(mode="after")
    def require_recorded(self) -> "PredictionToolOutput":
        if not self.recorded:
            raise ValueError("Prediction capture did not complete")
        return self
