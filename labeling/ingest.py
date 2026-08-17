# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from labeling.contracts import MAX_PREDICTION_BYTES, PredictionToolArguments, validate_prediction
from labeling.registry import resolve_parser
from labeling.store import (
    get_active_definition,
    get_binding_for_schedule,
    get_definition,
    insert_prediction,
)

logger = logging.getLogger(__name__)


def _structured_payload(entries: Iterable[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    captures: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("tool_name") != "record_predictions" or not entry.get("success", True):
            continue
        raw = entry.get("args_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                return None, "structured prediction arguments are invalid JSON"
        if isinstance(raw, dict):
            captures.append(raw)
    if not captures:
        return None, ""
    if len(captures) != 1:
        return None, "a run must contain exactly one prediction capture"
    try:
        return PredictionToolArguments.model_validate(captures[0]).predictions, ""
    except Exception as exc:
        logger.warning("structured prediction validation failed error=%s", type(exc).__name__)
        return None, "structured prediction arguments failed validation"


def _legacy_payload(output_text: str) -> dict[str, Any] | None:
    if not isinstance(output_text, str) or not output_text.strip():
        return None
    if len(output_text.encode("utf-8")) > MAX_PREDICTION_BYTES:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(output_text.lstrip())
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


async def ingest_scheduled_run_predictions(
    *,
    org_id: str,
    schedule_id: str,
    run_id: str,
    tool_call_log: Iterable[dict[str, Any]],
    output_text: str = "",
    prediction_at: datetime | None = None,
) -> str | None:
    structured, structured_error = _structured_payload(tool_call_log)
    predictions = None if structured_error else (structured or _legacy_payload(output_text))
    if predictions is None:
        if structured_error:
            logger.warning("prediction capture rejected run_id=%s reason=%s", run_id, structured_error)
        return None

    binding = await get_binding_for_schedule(schedule_id, org_id)
    if binding is not None:
        definition = await get_definition(
            str(binding["definition_key"]),
            int(binding["definition_version"]),
            active_only=False,
        )
        binding_id = str(binding["id"])
    else:
        task_class = predictions.get("task_class")
        definition = await get_active_definition(task_class) if isinstance(task_class, str) else None
        binding_id = None
    if definition is None:
        logger.info("prediction definition unavailable run_id=%s", run_id)
        return None

    captured_at = prediction_at or datetime.now(timezone.utc)
    parse_error = ""
    targets: list[Any] = []
    try:
        validate_prediction(predictions, definition.prediction_schema)
        parser = resolve_parser(definition.parser_key, definition.parser_version)
        targets = list(parser(predictions, definition, captured_at))
        if not targets:
            raise ValueError("prediction parser returned no targets")
    except Exception as exc:
        parse_error = "prediction could not be validated or expanded"
        logger.warning("prediction ingestion rejected run_id=%s error=%s", run_id, type(exc).__name__)

    prediction_id, _ = await insert_prediction(
        org_id=org_id,
        source_kind="scheduled_run",
        source_id=run_id,
        run_id=run_id,
        schedule_id=schedule_id,
        binding_id=binding_id,
        definition=definition,
        predictions=predictions,
        targets=targets if not parse_error else [],
        prediction_at=captured_at,
        parse_error=parse_error,
    )
    return prediction_id
