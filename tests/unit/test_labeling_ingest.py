# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from labeling import ingest
from labeling.contracts import Definition, TargetDraft


@pytest.mark.anyio
async def test_structured_ingest_is_idempotent_and_uses_exact_payload(monkeypatch):
    now = datetime.now(timezone.utc)
    definition = Definition(key="example", version=1, prediction_schema={"type": "object"})
    target = TargetDraft("root", {"value": 1}, now, now + timedelta(days=1), now + timedelta(days=1))
    monkeypatch.setattr(ingest, "get_binding_for_schedule", AsyncMock(return_value=None))
    monkeypatch.setattr(ingest, "get_active_definition", AsyncMock(return_value=definition))
    monkeypatch.setattr(ingest, "resolve_parser", lambda *_: lambda *_: [target])
    insert = AsyncMock(return_value=("prediction-1", True))
    monkeypatch.setattr(ingest, "insert_prediction", insert)

    payload = {"task_class": "example", "nested": {"exact": [1, None, True]}}
    result = await ingest.ingest_scheduled_run_predictions(
        org_id="org-1",
        schedule_id="schedule-1",
        run_id="run-1",
        tool_call_log=[
            {
                "tool_name": "record_predictions",
                "success": True,
                "args_json": '{"predictions":{"task_class":"example","nested":{"exact":[1,null,true]}}}',
            }
        ],
        output_text="human report must not be parsed",
        prediction_at=now,
    )

    assert result == "prediction-1"
    assert insert.await_args.kwargs["predictions"] == payload
    assert insert.await_args.kwargs["source_id"] == "run-1"


@pytest.mark.anyio
async def test_legacy_ingest_falls_back_to_json_prefix(monkeypatch):
    now = datetime.now(timezone.utc)
    definition = Definition(key="example", version=1, prediction_schema={"type": "object"})
    monkeypatch.setattr(ingest, "get_binding_for_schedule", AsyncMock(return_value=None))
    monkeypatch.setattr(ingest, "get_active_definition", AsyncMock(return_value=definition))
    monkeypatch.setattr(
        ingest,
        "resolve_parser",
        lambda *_: lambda *_: [TargetDraft("root", {"value": 1}, now, now + timedelta(days=1), now + timedelta(days=1))],
    )
    insert = AsyncMock(return_value=("prediction-2", True))
    monkeypatch.setattr(ingest, "insert_prediction", insert)

    result = await ingest.ingest_scheduled_run_predictions(
        org_id="org-1",
        schedule_id="schedule-1",
        run_id="run-2",
        tool_call_log=[],
        output_text='{"task_class":"example","value":1}\n---\nHuman report',
        prediction_at=now,
    )

    assert result == "prediction-2"
    assert insert.await_args.kwargs["predictions"] == {"task_class": "example", "value": 1}


@pytest.mark.anyio
async def test_invalid_structured_capture_does_not_fall_back_to_report_json(monkeypatch):
    insert = AsyncMock()
    monkeypatch.setattr(ingest, "insert_prediction", insert)

    result = await ingest.ingest_scheduled_run_predictions(
        org_id="org-1",
        schedule_id="schedule-1",
        run_id="run-3",
        tool_call_log=[
            {
                "tool_name": "record_predictions",
                "success": True,
                "args_json": '{"wrong_key": {"task_class": "example"}}',
            }
        ],
        output_text='{"task_class":"example","value":1}\n---\nHuman report',
    )

    assert result is None
    insert.assert_not_awaited()
