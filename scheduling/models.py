# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Scheduled run domain models."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class ScheduledRun(BaseModel):
    id: str
    org_id: str
    user_id: str
    name: str
    prompt: str
    schedule_kind: str = "interval"
    interval_seconds: int
    cron_expr: str | None = None
    enabled: bool = True
    callback_url: str | None = None
    next_run_at: datetime
    last_run_at: datetime | None = None
    consecutive_failures: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ScheduledRunResult(BaseModel):
    id: str
    schedule_id: str
    org_id: str
    run_id: str
    status: str
    output_text: str
    cost_usdc: int = 0
    error: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
