# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""A2A task projections for event-trigger runs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from scheduling.models import ScheduledRunResult

A2ATaskState = Literal[
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_REJECTED",
]

_STATE_MAP: dict[str, A2ATaskState] = {
    "completed": "TASK_STATE_COMPLETED",
    "failed": "TASK_STATE_FAILED",
    "timeout": "TASK_STATE_FAILED",
    "skipped": "TASK_STATE_REJECTED",
}


class EventTaskStatus(BaseModel):
    state: A2ATaskState
    timestamp: str


class EventTaskArtifactPart(BaseModel):
    text: str


class EventTaskArtifact(BaseModel):
    artifact_id: str = Field(alias="artifactId")
    name: str
    parts: list[EventTaskArtifactPart]

    model_config = {"populate_by_name": True}


class EventTaskResponse(BaseModel):
    id: str
    context_id: str = Field(alias="contextId")
    status: EventTaskStatus
    artifacts: list[EventTaskArtifact] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


def to_a2a_task_state(status: str | None) -> A2ATaskState:
    """Map persisted scheduler outcomes to the A2A task-state vocabulary."""
    if status is None:
        return "TASK_STATE_SUBMITTED"
    return _STATE_MAP.get(status, "TASK_STATE_FAILED")


def build_event_task(
    *,
    schedule_id: str,
    run_id: str,
    result: ScheduledRunResult | None,
) -> EventTaskResponse:
    timestamp = result.created_at if result is not None else datetime.now(timezone.utc)
    artifacts: list[EventTaskArtifact] = []
    if result is not None and result.output_text:
        artifacts.append(
            EventTaskArtifact(
                artifactId=f"{run_id}:result",
                name="result",
                parts=[EventTaskArtifactPart(text=result.output_text)],
            )
        )
    return EventTaskResponse(
        id=run_id,
        contextId=f"event-trigger:{schedule_id}",
        status=EventTaskStatus(
            state=to_a2a_task_state(result.status if result is not None else None),
            timestamp=timestamp.isoformat(),
        ),
        artifacts=artifacts,
        metadata={"scheduleId": schedule_id, "source": "event-trigger"},
    )
