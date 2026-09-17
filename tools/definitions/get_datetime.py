# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
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
    description=(
        "Return the current UTC date and time as a formatted string and ISO 8601 timestamp, for "
        "grounding time-relative reasoning in the actual clock rather than training-data assumptions."
    ),
    tags=["datetime", "utility"],
    use_when=(
        "Use when a task must anchor on the current date or time — computing ages, windows, deadlines, "
        "or 'as of today' context. The runtime context block already states the date, so call this only "
        "when a specific strftime format or a precise timestamp is needed."
    ),
    limitations=(
        "UTC only — no timezone conversion. An unsupported strftime format silently falls back to the "
        "default format instead of erroring."
    ),
    alternatives=["calculate", "get_block"],
    input_schema=GetDatetimeInput,
    output_schema=GetDatetimeOutput,
    show_on_agent_card=False,
    implementation=get_datetime,
)
