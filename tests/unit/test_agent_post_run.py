"""Focused tests for shared non-financial post-run telemetry."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from teardrop.agent_post_run import record_post_run_telemetry


@pytest.mark.anyio
async def test_record_post_run_telemetry_schedules_tool_and_memory_records():
    from teardrop import agent_post_run

    tool_events = AsyncMock()
    memory_extraction = AsyncMock()
    scheduled: list[object] = []

    def capture_task(coroutine):
        scheduled.append(coroutine)
        return MagicMock()

    with (
        patch.object(agent_post_run, "record_tool_call_events", tool_events),
        patch.object(agent_post_run, "extract_and_store_memories", memory_extraction),
        patch.object(agent_post_run.asyncio, "create_task", capture_task),
    ):
        record_post_run_telemetry(
            run_id="run-1",
            org_id="org-1",
            user_id="user-1",
            usage_data={
                "_tool_call_log": [{"tool_name": "platform/weather", "args_hash": "safe-hash"}],
                "billable_tool_names": ["platform/weather"],
            },
            state_values={"messages": ["older"] * 11, "slots": {"quotes": {"ETH": "safe"}}},
            settings=SimpleNamespace(tool_call_event_logging_enabled=True, memory_enabled=True),
            outcome=1,
            outcome_source="auto",
        )
        await scheduled[0]
        await scheduled[1]

    tool_events.assert_awaited_once_with("run-1", "org-1", [{"tool_name": "platform/weather", "args_hash": "safe-hash"}])
    memory_extraction.assert_awaited_once_with(
        "org-1",
        "user-1",
        ["older"] * 10,
        "run-1",
        tool_names_used=["platform/weather"],
        slots={"quotes": {"ETH": "safe"}},
        outcome=1,
        outcome_source="auto",
    )


@pytest.mark.anyio
async def test_record_post_run_telemetry_skips_disabled_or_missing_state():
    from teardrop import agent_post_run

    with (
        patch.object(agent_post_run, "record_tool_call_events", AsyncMock()) as tool_events,
        patch.object(agent_post_run, "extract_and_store_memories", AsyncMock()) as memory_extraction,
    ):
        record_post_run_telemetry(
            run_id="run-1",
            org_id="org-1",
            user_id="user-1",
            usage_data={"_tool_call_log": [{"tool_name": "weather"}]},
            state_values=None,
            settings=SimpleNamespace(tool_call_event_logging_enabled=False, memory_enabled=True),
        )

    tool_events.assert_not_awaited()
    memory_extraction.assert_not_awaited()
