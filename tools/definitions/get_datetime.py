# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""Get-datetime tool – current UTC date and time."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from tools.registry import ToolDefinition


# ─── Schemas ──────────────────────────────────────────────────────────────────


class GetDatetimeInput(BaseModel):
    format: str = Field(
        default="%Y-%m-%d %H:%M:%S UTC",
        description="strftime format string for the output",
        max_length=100,
    )


class GetDatetimeOutput(BaseModel):
    datetime_str: str = Field(..., alias="datetime")
    iso8601: str


# ─── Implementation ──────────────────────────────────────────────────────────


async def get_datetime(format: str = "%Y-%m-%d %H:%M:%S UTC") -> dict[str, str]:
    """Return the current UTC date and time in the requested format."""
    now = datetime.now(tz=timezone.utc)
    try:
        formatted = now.strftime(format)
    except Exception:
        formatted = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    return {"datetime": formatted, "iso8601": now.isoformat()}


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="get_datetime",
    version="1.0.0",
    description="Return the current UTC date and time. Optional strftime format parameter.",
    tags=["datetime", "utility"],
    input_schema=GetDatetimeInput,
    output_schema=GetDatetimeOutput,
    implementation=get_datetime,
)
